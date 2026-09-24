"""workflow.yaml -> Graph 编译器。

这是"模式 A 摸索出流程 -> 模式 B 固化"路径的落点：skill 作者只改 yaml，
就能把一段跑稳了的 agentic 流程换成确定性、可 checkpoint、可 resume 的图。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents.sub_agent import SubAgent, SubAgentSpec
from ..core.context import Ctx
from ..core.errors import GraphError, SkillError
from ..core.graph import END, Graph
from ..core.retry import RetryPolicy
from ..core.state import StateSchema
from ..tools.sandbox import run_script_json
from .spec import Skill

DEFAULT_RETRY = RetryPolicy(max_attempts=3, base=0.5, factor=2.0, max_delay=10.0)


def _retry_from(cfg: dict[str, Any] | None) -> RetryPolicy:
    if not cfg:
        return DEFAULT_RETRY
    if isinstance(cfg, int):
        return RetryPolicy(max_attempts=int(cfg), base=0.5)
    return RetryPolicy(
        max_attempts=int(cfg.get("max_attempts", 3)),
        base=float(cfg.get("base", 0.5)),
        factor=float(cfg.get("factor", 2.0)),
        max_delay=float(cfg.get("max_delay", 10.0)),
        jitter=bool(cfg.get("jitter", True)),
    )


def _script_node(skill: Skill, name: str, cfg: dict[str, Any]) -> Any:
    """脚本节点：stdin 收 {state, arg, run_dir}，stdout 吐 delta。

    能用代码做的就别用 LLM 做——去重、格式转换、统计走这条路，既省钱又确定。
    """
    rel = cfg.get("run")
    if not rel:
        raise SkillError(f"script 节点 {name!r} 缺少 run 字段")
    reads = cfg.get("reads")
    timeout = float(cfg.get("timeout", 60))
    writes = list(cfg.get("writes") or [])

    async def node(ctx: Ctx) -> dict[str, Any]:
        payload = {"state": ctx.state_slice(reads), "arg": ctx.arg,
                   "run_dir": str(ctx.run_dir), "step": ctx.step}
        script = skill.resource_path(rel)
        ctx.emit("script_run", script=rel, reads=reads)
        result = await ctx.effect(
            f"script:{rel}",
            lambda: run_script_json(script, payload, cwd=ctx.run_dir,
                                    timeout=timeout, allowed_root=skill.path))
        if not isinstance(result, dict):
            raise SkillError(f"脚本 {rel} 必须输出一个 JSON 对象，实际是 {type(result).__name__}")
        # 下划线前缀的 key 是诊断信息（统计、耗时），打到事件里而不是写进状态
        diagnostics = {k: v for k, v in result.items() if k.startswith("_")}
        result = {k: v for k, v in result.items() if not k.startswith("_")}
        if diagnostics:
            ctx.emit("script_stats", script=rel, **diagnostics)
        if writes:
            unexpected = set(result) - set(writes)
            if unexpected:
                raise SkillError(
                    f"脚本 {rel} 写了未声明的 channel {sorted(unexpected)}，"
                    f"workflow.yaml 里声明的是 {writes}")
        return result

    node.__name__ = f"script_{name}"
    return node


def compile_workflow(skill: Skill, max_steps: int | None = None) -> Graph:
    """把 skill 的 workflow.yaml 编译成已校验的 Graph。"""
    wf = skill.workflow
    if not wf:
        raise SkillError(f"skill {skill.name!r} 没有 workflow.yaml")

    schema = StateSchema.from_spec(wf.get("state") or {})
    graph = Graph(schema, name=f"{skill.name}:workflow")

    agents: dict[str, Any] = wf.get("agents") or {}
    scripts: dict[str, Any] = wf.get("scripts") or {}
    overlap = set(agents) & set(scripts)
    if overlap:
        raise SkillError(f"这些名字同时出现在 agents 和 scripts 里: {sorted(overlap)}")

    flow = wf.get("flow") or {}
    edges = flow.get("edges") or []
    # join 声明在边上，但生效于目标节点
    joins: dict[str, str] = {}
    for e in edges:
        if e.get("join"):
            joins[e["to"]] = e["join"]

    for name, cfg in agents.items():
        cfg = dict(cfg or {})
        spec = SubAgentSpec.from_yaml(name, cfg, skill)
        for channel in list(spec.output) + list(spec.const):
            if channel not in schema.names:
                raise SkillError(
                    f"agent {name!r} 声明输出到 channel {channel!r}，但 state 段里没有它")
        for channel in (spec.state_slice or []):
            if channel not in schema.names:
                raise SkillError(f"agent {name!r} 的 state_slice 引用了未声明的 channel {channel!r}")
        graph.add_node(
            name, SubAgent(spec, skill).as_node(),
            parallel_over=spec.parallel_over,
            join=joins.get(name, cfg.get("join", "any")),
            retry=_retry_from(cfg.get("retry")),
            timeout=cfg.get("timeout"),
            max_visits=int(cfg.get("max_visits", 25)),
            interrupt_before=bool(cfg.get("interrupt_before", False)),
            interrupt_after=bool(cfg.get("interrupt_after", False)),
            meta={"kind": "agent", "on_error": cfg.get("on_error")},
        )

    for name, cfg in scripts.items():
        cfg = dict(cfg or {})
        for channel in (cfg.get("writes") or []):
            if channel not in schema.names:
                raise SkillError(f"script {name!r} 声明写入未定义的 channel {channel!r}")
        graph.add_node(
            name, _script_node(skill, name, cfg),
            join=joins.get(name, cfg.get("join", "any")),
            retry=_retry_from(cfg.get("retry")),
            timeout=cfg.get("timeout"),
            interrupt_before=bool(cfg.get("interrupt_before", False)),
            meta={"kind": "script", "on_error": cfg.get("on_error")},
        )

    entry = flow.get("entry")
    if not entry:
        raise SkillError(f"skill {skill.name!r} 的 workflow.yaml 缺少 flow.entry")
    graph.set_entry(entry)

    for e in edges:
        src, dst = e.get("from"), e.get("to")
        if not src or not dst:
            raise SkillError(f"边定义不完整: {e}")
        graph.add_edge(src, END if dst == "END" else dst,
                       cond=e.get("if"), label=e.get("label", ""))

    try:
        graph.compile()
    except GraphError as e:
        raise SkillError(f"skill {skill.name!r} 的 workflow.yaml 编译失败: {e}") from e
    return graph


def compile_skill(skill: Skill, orchestrator_config: Any = None) -> Graph:
    """按 skill 的 mode 选择编排方式，返回可执行的图。"""
    from ..agents.orchestrator import build_agentic_graph
    if skill.mode == "workflow":
        return compile_workflow(skill)
    return build_agentic_graph(skill, orchestrator_config)
