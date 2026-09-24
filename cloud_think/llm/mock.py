"""测试用 LLM：脚本化响应 + 调用录制。

引擎和 skill 层的正确性必须能在不联网、不烧 token 的前提下验证，
否则每个 bug 都分不清是引擎问题还是模型问题。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .base import LLMResponse, Message, ToolCall, Usage, estimate_tokens

Matcher = str | Callable[[list[Message]], bool]
Reply = str | dict | list | LLMResponse | Callable[[list[Message]], Any]


@dataclass
class Rule:
    match: Matcher
    reply: Reply
    once: bool = False
    used: int = 0

    def matches(self, messages: list[Message]) -> bool:
        if self.once and self.used > 0:
            return False
        if callable(self.match):
            return bool(self.match(messages))
        blob = "\n".join(m.content for m in messages)
        return self.match in blob


class ScriptedLLM:
    """按规则匹配返回预设响应，同时记录所有调用便于断言。"""

    def __init__(self, rules: Sequence[Rule] | None = None, model: str = "mock",
                 default: Reply | None = None) -> None:
        self.rules = list(rules or [])
        self.model = model
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def add(self, match: Matcher, reply: Reply, once: bool = False) -> ScriptedLLM:
        self.rules.append(Rule(match, reply, once))
        return self

    async def complete(self, messages: Sequence[Message],
                       tools: Sequence[dict[str, Any]] | None = None,
                       **kwargs: Any) -> LLMResponse:
        msgs = list(messages)
        self.calls.append({"messages": [m.to_dict() for m in msgs],
                           "tools": [t.get("name") for t in (tools or [])]})
        for rule in self.rules:
            if rule.matches(msgs):
                rule.used += 1
                return self._render(rule.reply, msgs)
        if self.default is not None:
            return self._render(self.default, msgs)
        blob = "\n".join(m.content for m in msgs)[-400:]
        raise AssertionError(f"ScriptedLLM 没有匹配的规则。最后的消息片段:\n{blob}")

    def _render(self, reply: Reply, msgs: list[Message]) -> LLMResponse:
        if callable(reply) and not isinstance(reply, (str, dict, list)):
            reply = reply(msgs)
        if isinstance(reply, LLMResponse):
            resp = reply
        elif isinstance(reply, (dict, list)):
            resp = LLMResponse(text=json.dumps(reply, ensure_ascii=False))
        else:
            resp = LLMResponse(text=str(reply))
        resp.model = self.model
        tin = sum(estimate_tokens(m.content) for m in msgs)
        resp.usage = Usage(tokens_in=tin, tokens_out=estimate_tokens(resp.text))
        return resp


def tool_reply(name: str, args: dict[str, Any], call_id: str = "c1") -> LLMResponse:
    """构造一个"模型要求调用工具"的响应。"""
    return LLMResponse(text="", tool_calls=[ToolCall(id=call_id, name=name, args=args)])


@dataclass
class RecordingLLM:
    """包一层真实 client，把每次请求/响应落盘，便于把真实轨迹转成回归测试。"""

    inner: Any
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.inner.model

    async def complete(self, messages: Sequence[Message],
                       tools: Sequence[dict[str, Any]] | None = None,
                       **kwargs: Any) -> LLMResponse:
        resp = await self.inner.complete(messages, tools, **kwargs)
        self.log.append({"messages": [m.to_dict() for m in messages],
                         "response": resp.text, "usage": resp.usage.__dict__})
        return resp

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.log, f, ensure_ascii=False, indent=2)
