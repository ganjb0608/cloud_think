"""SubAgent：带上下文隔离的执行体。

多 agent 的价值不在"并行快"，在**上下文隔离**：一个 agent 把 20 个网页读进
上下文就废了；20 个 subagent 各读一个、各返回 300 字结论，主上下文只多 6k token。

三条硬规则由这里强制：
  1. 只注入 ``state_slice`` 声明的 channel，不给全量 state；
  2. 只加载 ``context_refs`` 声明的 reference，不把 skill 全量灌进去；
  3. 输出受 ``max_tokens_out`` 约束，长文走 artifact 落盘、state 里只留引用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.context import Ctx
from ..core.errors import OutputParseError
from ..llm.base import Message, estimate_tokens, extract_json
from ..tools.registry import ToolRegistry
from .tool_loop import tool_loop

OUTPUT_JSON = "json"
OUTPUT_TEXT = "text"
OUTPUT_ARTIFACT = "artifact"


@dataclass
class SubAgentSpec:
    """一个角色的完整契约。workflow.yaml 里的 ``agents:`` 段直接映射到这里。"""

    role: str
    prompt: str = ""
    state_slice: list[str] | None = None          # None = 不注入任何 state
    context_refs: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    output: dict[str, str] = field(default_factory=dict)   # channel -> json|text|artifact
    artifact_name: str = ""
    const: dict[str, Any] = field(default_factory=dict)   # 固定增量，如 {"revision": 1}
    key_by: str = ""                                      # "arg": 用 fan-out 的输入作为归并 key
    max_tokens_out: int = 2000
    max_tool_rounds: int = 8
    temperature: float = 0.2
    parallel_over: str | None = None

    @classmethod
    def from_yaml(cls, role: str, cfg: dict[str, Any], skill: Any = None) -> SubAgentSpec:
        cfg = dict(cfg or {})
        prompt = cfg.get("prompt_text", "")
        if not prompt and cfg.get("prompt") and skill is not None:
            prompt = skill.read_resource(cfg["prompt"])
        output = cfg.get("output") or {}
        if isinstance(output, str):
            output = {output: OUTPUT_TEXT}
        return cls(
            role=role, prompt=prompt,
            state_slice=cfg.get("state_slice"),
            context_refs=list(cfg.get("context") or []),
            tools=list(cfg.get("tools") or []),
            output=dict(output),
            artifact_name=cfg.get("artifact_name", ""),
            const=dict(cfg.get("const") or {}),
            key_by=str(cfg.get("key_by", "")),
            max_tokens_out=int(cfg.get("max_tokens_out", 2000)),
            max_tool_rounds=int(cfg.get("max_tool_rounds", 8)),
            temperature=float(cfg.get("temperature", 0.2)),
            parallel_over=cfg.get("parallel_over"),
        )


class SubAgent:
    """把一个 SubAgentSpec 变成可执行的图节点。"""

    def __init__(self, spec: SubAgentSpec, skill: Any = None) -> None:
        self.spec = spec
        self.skill = skill

    # ---------------------------------------------------------------- 提示装配
    def build_messages(self, ctx: Ctx, task: str = "") -> list[Message]:
        spec = self.spec
        skill = self.skill or ctx.skill
        parts: list[str] = []

        if skill is not None:
            parts.append(f"# 任务背景（skill: {skill.name}）\n\n{skill.body}")
        parts.append(f"ROLE: {spec.role}")
        if spec.prompt:
            parts.append(spec.prompt)

        # L3：只加载这个角色声明需要的 reference
        for ref in spec.context_refs:
            if skill is None:
                continue
            content = skill.read_resource(ref)
            parts.append(f"# 参考资料: {ref}\n\n{content}")
            ctx.emit("context_ref_loaded", role=spec.role, ref=ref, chars=len(content))

        if spec.output:
            parts.append(self._output_contract())

        system = "\n\n---\n\n".join(parts)

        user_parts: list[str] = []
        if spec.state_slice:
            user_parts.append("# 当前状态\n```json\n" +
                              ctx.state_json(spec.state_slice) + "\n```")
        if ctx.arg is not None:
            arg = ctx.arg if isinstance(ctx.arg, str) else json.dumps(ctx.arg, ensure_ascii=False)
            user_parts.append(f"# 你这一份的输入\n{arg}")
        if task:
            user_parts.append(f"# 你要做的事\n{task}")
        if not user_parts:
            user_parts.append("按你的角色说明开始工作。")

        return [Message("system", system), Message("user", "\n\n".join(user_parts))]

    def _output_contract(self) -> str:
        spec = self.spec
        kinds = set(spec.output.values())
        if kinds == {OUTPUT_TEXT} or kinds == {OUTPUT_ARTIFACT}:
            return ("# 输出要求\n直接输出正文内容本身，不要加任何解释、前言或代码围栏。")
        fields = "\n".join(f'  "{k}": …' for k, v in spec.output.items() if v == OUTPUT_JSON)
        return ("# 输出要求\n只输出一个 JSON 对象，放在 ```json 代码块里，不要任何额外解释：\n"
                "```json\n{\n" + fields + "\n}\n```")

    # ---------------------------------------------------------------- 执行
    async def run(self, ctx: Ctx, task: str = "") -> dict[str, Any]:
        spec = self.spec
        registry: ToolRegistry = ctx.tools or ToolRegistry()
        tools = registry.subset(spec.tools) if spec.tools else ToolRegistry()

        messages = self.build_messages(ctx, task)
        ctx.emit("subagent", role=spec.role, tools=tools.names(),
                 state_slice=spec.state_slice, refs=spec.context_refs,
                 prompt_tokens=sum(estimate_tokens(m.content) for m in messages))

        resp, _ = await tool_loop(ctx, messages, tools,
                                  max_rounds=spec.max_tool_rounds,
                                  temperature=spec.temperature)
        text = resp.text or ""

        out_tokens = estimate_tokens(text)
        if out_tokens > spec.max_tokens_out * 1.5:
            ctx.emit("subagent_output_oversized", role=spec.role,
                     tokens=out_tokens, limit=spec.max_tokens_out)

        return await self._parse(ctx, text)

    async def _parse(self, ctx: Ctx, text: str) -> dict[str, Any]:
        spec = self.spec
        if not spec.output and not spec.const:
            return {}

        delta: dict[str, Any] = {}
        json_channels = [k for k, v in spec.output.items() if v == OUTPUT_JSON]
        data: Any = None
        if json_channels:
            data = extract_json(text)

        for channel, kind in spec.output.items():
            if kind == OUTPUT_JSON:
                if isinstance(data, dict) and channel in data:
                    value = data[channel]
                elif len(json_channels) == 1:
                    value = data
                else:
                    raise OutputParseError(
                        f"角色 {spec.role!r} 的输出里缺少字段 {channel!r}，实际收到: "
                        f"{list(data) if isinstance(data, dict) else type(data).__name__}")
                if spec.key_by == "arg":
                    # fan-out 场景：用这个实例分到的输入做 key，配合 merge reducer
                    # 把 N 个实例的产出归并到一个 dict，不依赖模型自己填对 key
                    key = ctx.arg if isinstance(ctx.arg, str) else json.dumps(
                        ctx.arg, ensure_ascii=False, sort_keys=True)
                    value = {key: value}
                delta[channel] = value
            elif kind == OUTPUT_ARTIFACT:
                name = spec.artifact_name or f"{spec.role}_step{ctx.step}.md"
                if "{" in name:
                    name = name.format(step=ctx.step, role=spec.role,
                                       instance=ctx.instance, **dict(ctx.state))
                delta[channel] = await ctx.write_artifact(name, text)
            else:
                delta[channel] = text
        for channel, value in spec.const.items():
            delta.setdefault(channel, value)
        return delta

    def as_node(self) -> Any:
        """包装成图节点的执行体。"""
        async def _node(ctx: Ctx) -> dict[str, Any]:
            return await self.run(ctx)
        _node.__name__ = f"subagent_{self.spec.role}"
        return _node
