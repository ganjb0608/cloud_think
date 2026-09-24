"""模式 A：agentic 软编排。

SKILL.md 就是给 orchestrator 看的流程说明，它自己决定分派哪些子任务。
适合每次形态都不一样的任务（排障、开放式研究）。代价是不确定、token 消耗高——
所以流程稳定下来之后应该固化成 workflow.yaml 换确定性。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.context import Ctx
from ..core.graph import END, Graph
from ..core.state import StateSchema
from ..llm.base import Message
from ..tools.registry import Tool, ToolRegistry
from .sub_agent import SubAgent, SubAgentSpec
from .tool_loop import tool_loop

#: agentic 模式的通用状态。skill 不用声明 schema 也能跑。
AGENTIC_STATE = {
    "task": {"type": "str"},
    "notes": {"type": "list", "reducer": "extend"},
    "artifacts": {"type": "dict", "reducer": "merge"},
    "result": {"type": "str"},
    "rounds": {"type": "int", "reducer": "add"},
}

ORCHESTRATOR_PROMPT = """你是一个编排者，负责按上面的 skill 流程完成用户任务。

你自己不做具体调研或写作——你把工作拆给子 agent。每个子 agent 有独立上下文，
只能看到你交给它的任务描述，所以任务描述要自足。

可用工具：
- spawn_subagent(role, task, tools, context_refs): 派一个子 agent 干一件事，返回它的结论
- read_reference(name): 读 skill 的参考文档
- run_script(name, payload): 跑 skill 的脚本做确定性处理
- write_artifact(name, content): 把长文落盘，返回引用
- finish(result, notes): 交付最终结果，结束任务

原则：
- 能并行的子任务一次派多个，不要串行等待
- 子 agent 的结论要精炼，长文让它写进 artifact，只把引用带回来
- 能用脚本做的（去重、格式转换、统计）就别让 LLM 做
- 完成后必须调用 finish"""


@dataclass
class OrchestratorConfig:
    max_rounds: int = 12
    subagent_tools: list[str] = field(default_factory=list)
    default_max_tokens_out: int = 1500


class Orchestrator:
    def __init__(self, skill: Any, config: OrchestratorConfig | None = None) -> None:
        self.skill = skill
        self.config = config or OrchestratorConfig()

    def _tools(self, ctx: Ctx) -> ToolRegistry:
        base: ToolRegistry = ctx.tools or ToolRegistry()
        allowed = self.config.subagent_tools or self.skill.meta.tools
        reg = ToolRegistry()
        state: dict[str, Any] = {"finished": None, "notes": [], "artifacts": {}}
        ctx.deps["_orch_state"] = state

        async def spawn_subagent(role: str, task: str, tools: list[str] | None = None,
                                 context_refs: list[str] | None = None) -> str:
            """派子 agent。它的工具调用和长文读取都留在它自己的上下文里。"""
            wanted = [t for t in (tools or []) if t in allowed]
            spec = SubAgentSpec(
                role=role, tools=wanted, context_refs=list(context_refs or []),
                output={"_text": "text"},
                max_tokens_out=self.config.default_max_tokens_out,
            )
            child = Ctx(
                run_id=ctx.run_id, step=ctx.step, node=ctx.node,
                instance=f"{ctx.instance}/{role}", state={}, arg=None,
                run_dir=ctx.run_dir, cp=ctx.cp, bus=ctx.bus, deps=ctx.deps,
            )
            result = await SubAgent(spec, self.skill).run(child, task)
            ctx.add_usage(child.usage["tokens_in"], child.usage["tokens_out"])
            return str(result.get("_text", ""))

        async def write_artifact(name: str, content: str) -> str:
            ref = await ctx.write_artifact(name, content)
            state["artifacts"][name] = ref
            return ref

        def finish(result: str, notes: list[str] | None = None) -> str:
            state["finished"] = result
            state["notes"] = list(notes or [])
            return "已记录最终结果。"

        reg.register(Tool("spawn_subagent",
                          "派一个子 agent 执行一件具体的事，返回它的结论。"
                          "参数 role（角色名）、task（自足的任务描述）、"
                          "tools（它能用的工具名列表）、context_refs（它要读的参考文档）。",
                          {"type": "object", "properties": {
                              "role": {"type": "string"}, "task": {"type": "string"},
                              "tools": {"type": "array", "items": {"type": "string"}},
                              "context_refs": {"type": "array", "items": {"type": "string"}}},
                           "required": ["role", "task"]},
                          spawn_subagent))
        reg.register(Tool("write_artifact", "把长文写入 run 目录，返回引用。参数 name、content。",
                          {"type": "object", "properties": {
                              "name": {"type": "string"}, "content": {"type": "string"}},
                           "required": ["name", "content"]},
                          write_artifact))
        reg.register(Tool("finish", "交付最终结果并结束。参数 result、notes。",
                          {"type": "object", "properties": {
                              "result": {"type": "string"},
                              "notes": {"type": "array", "items": {"type": "string"}}},
                           "required": ["result"]},
                          finish))
        for name in ("read_reference", "run_script"):
            if name in base:
                reg.register(base.get(name))
        return reg

    async def run(self, ctx: Ctx) -> dict[str, Any]:
        tools = self._tools(ctx)
        state = ctx.deps["_orch_state"]
        system = f"# skill: {self.skill.name}\n\n{self.skill.body}\n\n---\n\n{ORCHESTRATOR_PROMPT}"
        task = ctx.state.get("task", "")
        messages = [Message("system", system), Message("user", f"# 用户任务\n{task}")]

        resp, history = await tool_loop(ctx, messages, tools,
                                        max_rounds=self.config.max_rounds)
        result = state["finished"]
        if result is None:
            # 模型没调 finish 就收尾了：用最后一次输出兜底，但记下来
            ctx.emit("orchestrator_no_finish", last=resp.text[:200])
            result = resp.text
        return {
            "result": result,
            "notes": list(state["notes"]),
            "artifacts": dict(state["artifacts"]),
            "rounds": 1,
        }


def build_agentic_graph(skill: Any, config: OrchestratorConfig | None = None) -> Graph:
    """把一个 agentic skill 包成单节点图，这样它和 workflow 模式共用同一套
    checkpoint / resume / 事件设施。"""
    schema = StateSchema.from_spec(AGENTIC_STATE)
    graph = Graph(schema, name=f"{skill.name}:agentic")
    orch = Orchestrator(skill, config)

    async def node(ctx: Ctx) -> dict[str, Any]:
        return await orch.run(ctx)

    graph.add_node("orchestrator", node, max_visits=2)
    graph.set_entry("orchestrator")
    graph.add_edge("orchestrator", END)
    return graph.compile()
