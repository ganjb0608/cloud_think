"""LLM 客户端抽象。本地模型与云 API 走同一个协议，可随时互换。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ..core.errors import OutputParseError


@dataclass
class Message:
    role: str                     # system | user | assistant | tool
    content: str = ""
    tool_call_id: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.tokens_in + other.tokens_in, self.tokens_out + other.tokens_out)


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw: Any = None


class LLMClient(Protocol):
    model: str

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    """粗略 token 估计。中文约 1.5 字/token，英文约 4 字符/token，取个折中。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[一-鿿]", text))
    return int(cjk / 1.5) + max(0, (len(text) - cjk)) // 4


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """从 LLM 输出里抠出 JSON。

    依次尝试：整体解析 -> 最后一个围栏代码块 -> 第一个平衡的 {...} / [...]。
    模型爱在 JSON 前后加解释，这三层兜底能挡住绝大多数情况。
    """
    text = (text or "").strip()
    if not text:
        raise OutputParseError("LLM 返回空内容，无法解析 JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for block in reversed(_FENCE.findall(text)):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise OutputParseError(f"无法从 LLM 输出中解析出 JSON，开头是: {text[:200]!r}")
