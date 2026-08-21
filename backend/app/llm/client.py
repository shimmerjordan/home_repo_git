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


class LLMTruncated(LLMError):
    """输出被 max_tokens 截断。

    单拎出来是因为它可以**重试救回来**, 而其它 LLMError 不能。
    截断最恶劣的形态是 tool_use 的 input JSON 断在一半 —— 服务端返回的
    arguments 会缺字段甚至整个为空, 于是"我用完一瓶水、拿了螺丝刀和卷尺"这类
    多物品语句会静默丢掉全部 operations。以前这里不检查 stop_reason,
    截断结果被当成正常解析结果往下走, 这就是多物品操作老是不对的头号原因。
    """

    def __init__(self, message: str, *, max_tokens: int = 0):
        super().__init__(message)
        self.max_tokens = max_tokens


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

    def _effective_max_tokens(self, override: int | None) -> int:
        return int(override or getattr(self.cfg, "max_tokens", 0) or 1024)

    def _can_force_tool(self) -> bool:
        """能不能把 tool_choice 钉死到某个工具上。

        Anthropic 侧有个硬限制: **扩展思考开着的时候不允许强制工具调用**。
        实测 claude-opus-4-8 + thinking 未显式关闭 + tool_choice={"type":"tool"}
        会被网关直接拒掉 (返回一张 404 HTML 页, 连 JSON 错误都不是)。
        所以只有 thinking 明确 disabled 时才敢钉死; 其余情况退回 auto ——
        思考对多物品语句的拆分准确度是有帮助的, 不值得为了钉工具把它关掉。
        OpenAI 协议没有这个约束。
        """
        if not self._is_anthropic:
            return True
        return getattr(self.cfg, "thinking", "") == "disabled"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        force_json: bool = False,
        force_tool: str = "",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Return a normalized result: {content: str, tool_calls: list[{name, arguments(dict)}]}.

        force_tool: 工具名。给了就尽量把 tool_choice 钉死到它上面, 省掉"模型改用散文回答
        → 再发一轮 chat_json"那条又慢又不准的退路。受 _can_force_tool() 约束。
        max_tokens: 覆盖配置里的值 —— 截断重试时用更大的预算再打一次。
        """
        use_tools = bool(tools) and self.cfg.supports_tools
        pin_tool = bool(force_tool) and use_tools and self._can_force_tool()
        if self._is_anthropic:
            # Anthropic /v1/messages: system 是顶层字段, max_tokens 必填。
            # 不传 temperature —— Claude 新家族 (Opus 4.7+/Sonnet 5/Fable 5) 已移除该参数, 传了直接 400;
            # 关键参数只有 model + thinking(mode) + output_config.effort (均可选, 留空不传)。
            system_parts = [m.get("content") or "" for m in messages if m.get("role") == "system"]
            msgs = [
                {"role": m["role"], "content": m.get("content") or ""}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ]
            payload = {
                "model": self.cfg.model,
                "messages": msgs,
                "max_tokens": self._effective_max_tokens(max_tokens),
            }
            thinking = getattr(self.cfg, "thinking", "")
            if thinking in ("adaptive", "disabled"):
                payload["thinking"] = {"type": thinking}
            effort = getattr(self.cfg, "effort", "")
            if effort:
                payload["output_config"] = {"effort": effort}
            if system_parts:
                payload["system"] = "\n\n".join(p for p in system_parts if p)
            if use_tools:
                payload["tools"] = self._tools_to_anthropic(tools)
                payload["tool_choice"] = (
                    {"type": "tool", "name": force_tool} if pin_tool else {"type": "auto"}
                )
            # force_json: Anthropic 没有 response_format, 依赖 prompt 约束 + _strip_json。
        else:
            payload = {
                "model": self.cfg.model,
                "messages": messages,
                "temperature": self.cfg.temperature,
            }
            payload["max_tokens"] = self._effective_max_tokens(max_tokens)
            if use_tools:
                payload["tools"] = tools
                payload["tool_choice"] = (
                    {"type": "function", "function": {"name": force_tool}}
                    if pin_tool else "auto"
                )
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
            if pin_tool:
                # 钉死工具是个优化, 不该成为单点故障: 有些网关/模型组合直接拒掉这种请求
                # (实测会返回 404 HTML)。退回 auto 再打一次, 别让整条语音链路挂在这。
                log.warning("LLM 拒绝了钉死工具的请求, 退回 tool_choice=auto 重试")
                return await self.chat(messages, tools=tools, force_json=force_json,
                                       force_tool="", max_tokens=max_tokens)
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"LLM 返回的不是 JSON: {resp.text[:300]}") from exc
        usage = data.get("usage") or {}
        budget = self._effective_max_tokens(max_tokens)
        if self._is_anthropic:
            stop_reason = data.get("stop_reason") or ""
            log.info("LLM ok %d %.0fms tokens=%s stop=%s",
                     resp.status_code, elapsed_ms,
                     f"{usage.get('input_tokens','?')}/{usage.get('output_tokens','?')}",
                     stop_reason)
            if stop_reason == "max_tokens":
                raise LLMTruncated(
                    f"输出在 max_tokens={budget} 处被截断 "
                    f"(output_tokens={usage.get('output_tokens','?')})",
                    max_tokens=budget,
                )
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

        if choice.get("finish_reason") == "length":
            raise LLMTruncated(
                f"输出在 max_tokens={budget} 处被截断 "
                f"(completion_tokens={usage.get('completion_tokens','?')})",
                max_tokens=budget,
            )

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
        self, messages: list[dict[str, Any]], *, schema_hint: str = "",
        max_tokens: int | None = None,
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
        result = await self.chat(msgs, force_json=True, max_tokens=max_tokens)
        text = _strip_json(result["content"])
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model did not return valid JSON: {result['content'][:500]}") from exc
