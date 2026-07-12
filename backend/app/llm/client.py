"""Generic OpenAI-compatible chat client.

Works with OpenAI, Ollama (>=0.1.30), SiliconFlow, DeepSeek, Together, and any provider that
exposes the /v1/chat/completions endpoint. When the provider does not support tool calling we
fall back to JSON-mode prompting (instructing the model to emit a single JSON object).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from ..config import LLMConfig

log = logging.getLogger("storage.llm")


class LLMError(RuntimeError):
    pass


def _strip_json(text: str) -> str:
    """Extract a JSON object from the model output, tolerating ```json fences and prose."""
    text = text.strip()
    # Remove ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        return fence.group(1)
    # First top-level JSON object.
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    @property
    def _is_anthropic(self) -> bool:
        return getattr(self.cfg, "api_format", "openai") == "anthropic"

    @property
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._is_anthropic:
            h["anthropic-version"] = "2023-06-01"
            if self.cfg.api_key:
                # 官方要求 x-api-key; cc-trans 等网关同时接受 x-api-key 和 Bearer。
                h["x-api-key"] = self.cfg.api_key
        elif self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    @property
    def _url(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if self._is_anthropic:
            # 用户可能填 https://api.anthropic.com 或 http://nas:8787/v1 — 统一成 /v1/messages。
            if base.endswith("/v1"):
                base = base[: -len("/v1")].rstrip("/")
            return base + "/v1/messages"
        return base + "/chat/completions"

    @staticmethod
    def _tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI function-tools → Anthropic tool schema."""
        out = []
        for t in tools:
            fn = t.get("function") or {}
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object"},
            })
        return out

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> dict[str, Any]:
        """Return a normalized result: {content: str, tool_calls: list[{name, arguments(dict)}]}."""
        use_tools = bool(tools) and self.cfg.supports_tools
        if self._is_anthropic:
            # Anthropic /v1/messages: system 是顶层字段, max_tokens 必填, temperature ≤ 1。
            system_parts = [m.get("content") or "" for m in messages if m.get("role") == "system"]
            msgs = [
                {"role": m["role"], "content": m.get("content") or ""}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ]
            payload = {
                "model": self.cfg.model,
                "messages": msgs,
                "max_tokens": int(getattr(self.cfg, "max_tokens", 0) or 1024),
                "temperature": min(self.cfg.temperature, 1.0),
            }
            if system_parts:
                payload["system"] = "\n\n".join(p for p in system_parts if p)
            if use_tools:
                payload["tools"] = self._tools_to_anthropic(tools)
                payload["tool_choice"] = {"type": "auto"}
            # force_json: Anthropic 没有 response_format, 依赖 prompt 约束 + _strip_json。
        else:
            payload = {
                "model": self.cfg.model,
                "messages": messages,
                "temperature": self.cfg.temperature,
            }
            if getattr(self.cfg, "max_tokens", 0):
                payload["max_tokens"] = int(self.cfg.max_tokens)
            if use_tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            elif force_json:
                # Many OpenAI-compatible servers honor this; safe to set even if ignored.
                payload["response_format"] = {"type": "json_object"}

        log.debug("LLM request → %s model=%s tools=%s msgs=%d",
                  self._url, self.cfg.model, use_tools, len(messages))
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
                resp = await client.post(self._url, headers=self._headers, json=payload)
        except httpx.HTTPError as exc:
            log.error("LLM request failed: %s", exc)
            raise LLMError(f"LLM request failed: {exc}") from exc

        elapsed_ms = (time.time() - started) * 1000
        if resp.status_code >= 400:
            log.error("LLM HTTP %d after %.0fms: %s", resp.status_code, elapsed_ms, resp.text[:500])
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        usage = data.get("usage") or {}
        if self._is_anthropic:
            log.info("LLM ok %d %.0fms tokens=%s",
                     resp.status_code, elapsed_ms,
                     f"{usage.get('input_tokens','?')}/{usage.get('output_tokens','?')}")
            blocks = data.get("content")
            if not isinstance(blocks, list):
                raise LLMError(f"Unexpected LLM response shape: {data}")
            content = ""
            tool_calls_norm: list[dict[str, Any]] = []
            for block in blocks:
                btype = block.get("type")
                if btype == "text":
                    content += block.get("text") or ""
                elif btype == "tool_use":
                    tool_calls_norm.append({
                        "name": block.get("name", ""),
                        "arguments": block.get("input") or {},
                    })
            return {"content": content, "tool_calls": tool_calls_norm, "raw": data}

        log.info("LLM ok %d %.0fms tokens=%s",
                 resp.status_code, elapsed_ms,
                 f"{usage.get('prompt_tokens','?')}/{usage.get('completion_tokens','?')}")
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {data}") from exc

        content = message.get("content") or ""
        tool_calls_norm = []

        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw or "{}")
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw or {}
            tool_calls_norm.append({"name": name, "arguments": args})

        return {"content": content, "tool_calls": tool_calls_norm, "raw": data}

    async def chat_json(
        self, messages: list[dict[str, Any]], *, schema_hint: str = ""
    ) -> dict[str, Any]:
        """Convenience: ask for strict JSON, parse it. Schema_hint is appended to last user msg."""
        msgs = [dict(m) for m in messages]
        if schema_hint:
            msgs.append({
                "role": "system",
                "content": (
                    "You MUST respond with a single valid JSON object and nothing else. "
                    "Schema:\n" + schema_hint
                ),
            })
        result = await self.chat(msgs, force_json=True)
        text = _strip_json(result["content"])
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model did not return valid JSON: {result['content'][:500]}") from exc
