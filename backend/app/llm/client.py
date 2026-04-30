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
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    @property
    def _url(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
    ) -> dict[str, Any]:
        """Return a normalized result: {content: str, tool_calls: list[{name, arguments(dict)}]}."""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
        }

        use_tools = bool(tools) and self.cfg.supports_tools
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
        log.info("LLM ok %d %.0fms tokens=%s",
                 resp.status_code, elapsed_ms,
                 f"{usage.get('prompt_tokens','?')}/{usage.get('completion_tokens','?')}")
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {data}") from exc

        content = message.get("content") or ""
        tool_calls_norm: list[dict[str, Any]] = []

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
