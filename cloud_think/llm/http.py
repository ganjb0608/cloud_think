"""本地/远端 LLM 的 HTTP 客户端。只用标准库，不引第三方依赖。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Sequence

import asyncio

from .base import LLMResponse, Message, ToolCall, Usage, estimate_tokens


class LLMHTTPError(RuntimeError):
    """可重试的 LLM 传输层错误（超时、5xx、429）。"""


def _post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if e.code in (408, 429) or e.code >= 500:
            raise LLMHTTPError(f"HTTP {e.code}: {detail}") from e
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise LLMHTTPError(f"连接失败: {e.reason}") from e


class OllamaClient:
    """本地 Ollama。路由、审稿这类短任务跑本地小模型，省钱又快。"""

    def __init__(self, model: str = "qwen2.5:7b",
                 host: str = "http://127.0.0.1:11434", timeout: float = 300.0) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    async def complete(self, messages: Sequence[Message],
                       tools: Sequence[dict[str, Any]] | None = None,
                       **kwargs: Any) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
            "options": {"temperature": kwargs.get("temperature", 0.2)},
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        data = await asyncio.to_thread(_post, f"{self.host}/api/chat", payload, {}, self.timeout)
        msg = data.get("message", {})
        calls = [ToolCall(id=f"c{i}", name=tc["function"]["name"],
                          args=tc["function"].get("arguments") or {})
                 for i, tc in enumerate(msg.get("tool_calls") or [])]
        return LLMResponse(
            text=msg.get("content", ""), tool_calls=calls, model=self.model, raw=data,
            usage=Usage(tokens_in=data.get("prompt_eval_count", 0),
                        tokens_out=data.get("eval_count", 0)))


class AnthropicClient:
    """Claude Messages API。"""

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None,
                 base_url: str = "https://api.anthropic.com", max_tokens: int = 4096,
                 timeout: float = 300.0) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout

    async def complete(self, messages: Sequence[Message],
                       tools: Sequence[dict[str, Any]] | None = None,
                       **kwargs: Any) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("缺少 ANTHROPIC_API_KEY")
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        convo: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                convo.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}]})
            else:
                convo.append({"role": m.role, "content": m.content})
        payload: dict[str, Any] = {
            "model": self.model, "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "messages": convo or [{"role": "user", "content": "continue"}],
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                                 "input_schema": t.get("parameters", {"type": "object"})}
                                for t in tools]
        data = await asyncio.to_thread(
            _post, f"{self.base_url}/v1/messages", payload,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, self.timeout)
        text, calls = "", []
        for i, block in enumerate(data.get("content", [])):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block.get("id", f"c{i}"), name=block["name"],
                                      args=block.get("input") or {}))
        u = data.get("usage", {})
        return LLMResponse(text=text, tool_calls=calls, model=self.model, raw=data,
                           usage=Usage(u.get("input_tokens", 0), u.get("output_tokens", 0)))


class OpenAICompatClient:
    """任何 OpenAI 兼容端点：vLLM / LM Studio / llama.cpp server / 各类网关。"""

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:8000/v1",
                 api_key: str | None = None, timeout: float = 300.0) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "sk-noauth")
        self.timeout = timeout

    async def complete(self, messages: Sequence[Message],
                       tools: Sequence[dict[str, Any]] | None = None,
                       **kwargs: Any) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model, "messages": [m.to_dict() for m in messages],
            "temperature": kwargs.get("temperature", 0.2),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        data = await asyncio.to_thread(
            _post, f"{self.base_url}/chat/completions", payload,
            {"Authorization": f"Bearer {self.api_key}"}, self.timeout)
        choice = (data.get("choices") or [{}])[0].get("message", {})
        calls = [ToolCall(id=tc.get("id", f"c{i}"), name=tc["function"]["name"],
                          args=json.loads(tc["function"].get("arguments") or "{}"))
                 for i, tc in enumerate(choice.get("tool_calls") or [])]
        u = data.get("usage", {})
        text = choice.get("content") or ""
        return LLMResponse(text=text, tool_calls=calls, model=self.model, raw=data,
                           usage=Usage(u.get("prompt_tokens", estimate_tokens(text)),
                                       u.get("completion_tokens", 0)))
