"""装配层：把 skill 层、agent 层、引擎、工具、存储接到一起。

一个复杂任务进来的完整路径：
  route -> 加载 SKILL.md -> 按 mode 编译成图 -> 引擎执行 -> checkpoint/resume
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .core.checkpoint import Checkpointer, SQLiteCheckpointer
from .core.engine import Engine, RunResult
from .core.errors import RoutingError
from .core.events import ConsoleSink, EventBus, JsonlSink
from .core.graph import Graph
from .skills.compiler import compile_skill
from .skills.registry import SkillRegistry
from .skills.router import RouteResult, SkillRouter
from .skills.spec import Skill
from .tools.registry import ToolRegistry


@dataclass
class RuntimeConfig:
    skill_roots: list[str | Path] = field(default_factory=lambda: ["skills"])
    db_path: str | Path = "state.db"
    run_root: str | Path = "runs"
    max_steps: int = 50
    verbose: bool = False
    console: bool = True


class Runtime:
    def __init__(
        self, llm: Any, tools: ToolRegistry | None = None,
        config: RuntimeConfig | None = None,
        checkpointer: Checkpointer | None = None,
        bus: EventBus | None = None,
        registry: SkillRegistry | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.cp = checkpointer or SQLiteCheckpointer(self.config.db_path)
        self.bus = bus or EventBus()
        if self.config.console:
            self.bus.add(ConsoleSink(verbose=self.config.verbose))
        if hasattr(self.cp, "handle"):
            self.bus.add(self.cp)
        self.registry = registry or SkillRegistry(self.config.skill_roots)
        self.router = SkillRouter(self.registry, llm=llm, bus=self.bus)

    # ---------------------------------------------------------------- 编排
    def engine_for(self, skill: Skill, graph: Graph | None = None) -> Engine:
        graph = graph or compile_skill(skill)
        return Engine(
            graph, self.cp, self.bus,
            run_root=self.config.run_root,
            max_steps=min(self.config.max_steps, skill.meta.max_steps),
            deps={"llm": self.llm, "tools": self.tools, "skill": skill,
                  "registry": self.registry},
        )

    def _task_channel(self, skill: Skill, graph: Graph) -> str | None:
        """任务文本注入哪个 channel。"""
        wf = skill.workflow or {}
        if wf.get("task_channel"):
            return wf["task_channel"]
        names = graph.schema.names
        for cand in ("task", "topic", "question", "input"):
            if cand in names:
                return cand
        for name, ch in graph.schema.channels.items():
            if ch.type == "str":
                return name
        return None

    # ---------------------------------------------------------------- 入口
    async def route(self, task: str) -> tuple[RouteResult, list[Skill]]:
        result = await self.router.route(task)
        return result, self.router.resolve(result)

    async def run_task(
        self, task: str, inputs: Mapping[str, Any] | None = None,
        skill_name: str | None = None, run_id: str | None = None,
    ) -> RunResult:
        """完整路径：路由 -> 编译 -> 执行。"""
        if skill_name:
            skill = self.registry.get(skill_name)
            via = "explicit"
        else:
            result, skills = await self.route(task)
            if not skills:
                raise RoutingError(
                    f"没有匹配的 skill。已安装: {self.registry.names()}。"
                    f"候选打分: {[(m.name, m.score) for m in result.candidates[:3]]}")
            skill = skills[0]
            via = result.via
        return await self.run_skill(skill, task, inputs, run_id=run_id, via=via)

    async def run_skill(
        self, skill: Skill | str, task: str = "",
        inputs: Mapping[str, Any] | None = None,
        run_id: str | None = None, via: str = "explicit",
    ) -> RunResult:
        if isinstance(skill, str):
            skill = self.registry.get(skill)
        graph = compile_skill(skill)
        engine = self.engine_for(skill, graph)

        payload = dict(inputs or {})
        channel = self._task_channel(skill, graph)
        if task and channel and channel not in payload:
            payload[channel] = task

        run_dir = Path(self.config.run_root)
        if run_id:
            self.bus.add(JsonlSink(run_dir / run_id / "trace.jsonl"))
        self.bus.emit("skill_selected", skill=skill.name, mode=skill.mode, via=via,
                      task_channel=channel, nodes=sorted(graph.nodes))
        return await engine.invoke(payload, run_id=run_id,
                                   meta={"skill": skill.name, "mode": skill.mode,
                                         "task": task, "via": via})

    async def resume(self, run_id: str, answer: Any = None) -> RunResult:
        skill = await self._skill_of(run_id)
        return await self.engine_for(skill).resume(run_id, answer=answer)

    async def fork(self, run_id: str, from_step: int,
                   overrides: Mapping[str, Any] | None = None) -> RunResult:
        skill = await self._skill_of(run_id)
        return await self.engine_for(skill).fork(run_id, from_step, overrides)

    async def _skill_of(self, run_id: str) -> Skill:
        run = await self.cp.get_run(run_id)
        if run is None:
            raise KeyError(f"未知 run {run_id!r}")
        meta = run.get("meta")
        if isinstance(meta, str):
            import json
            meta = json.loads(meta or "{}")
        name = (meta or {}).get("skill")
        if not name:
            raise KeyError(f"run {run_id!r} 没有记录所属 skill")
        return self.registry.get(name)
