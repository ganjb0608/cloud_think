"""工具注册表。每个 skill / subagent 只拿到自己白名单内的工具。"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    fn: Callable[..., Any] = None  # type: ignore[assignment]
    dangerous: bool = False        # 需要显式授权才会进白名单

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    async def call(self, ctx: Any, **kwargs: Any) -> Any:
        sig = inspect.signature(self.fn)
        if "ctx" in sig.parameters:
            kwargs = {"ctx": ctx, **kwargs}
        result = self.fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self.tools: dict[str, Tool] = {t.name: t for t in (tools or [])}

    def register(self, tool: Tool) -> ToolRegistry:
        self.tools[tool.name] = tool
        return self

    def add(self, name: str, description: str, fn: Callable[..., Any],
            parameters: dict[str, Any] | None = None, dangerous: bool = False) -> ToolRegistry:
        return self.register(Tool(name, description,
                                  parameters or {"type": "object", "properties": {}},
                                  fn, dangerous))

    def get(self, name: str) -> Tool:
        if name not in self.tools:
            raise KeyError(f"未知工具 {name!r}，已注册: {sorted(self.tools)}")
        return self.tools[name]

    def subset(self, names: Iterable[str] | None) -> ToolRegistry:
        """按白名单裁剪。names 为 None 表示不给任何工具（默认最小权限）。"""
        if names is None:
            return ToolRegistry()
        wanted = list(names)
        missing = [n for n in wanted if n not in self.tools]
        if missing:
            raise KeyError(f"skill 请求了未注册的工具: {missing}，已注册: {sorted(self.tools)}")
        return ToolRegistry([self.tools[n] for n in wanted])

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def __len__(self) -> int:
        return len(self.tools)

    def __contains__(self, name: object) -> bool:
        return name in self.tools

    def names(self) -> list[str]:
        return sorted(self.tools)
