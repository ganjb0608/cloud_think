"""工作流图：节点、边、路由规则，以及编译期校验。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .errors import GraphError
from .expr import safe_eval, validate_expr
from .retry import NO_RETRY, RetryPolicy
from .state import Delta, StateSchema

END = "__end__"
START = "__start__"


class AgentFn(Protocol):
    """节点执行体：拿到 ctx，返回状态增量。"""

    def __call__(self, ctx: Any) -> Awaitable[Delta]: ...


@dataclass
class NodeSpec:
    name: str
    fn: AgentFn
    retry: RetryPolicy = NO_RETRY
    timeout: float | None = None
    join: str = "any"                      # any: 任一上游到达即触发; all: 等齐所有上游
    max_visits: int = 25
    parallel_over: str | None = None       # 声明式 fan-out：按该 channel 的列表展开
    router: Callable[[Mapping[str, Any]], str | list[str]] | None = None
    interrupt_before: bool = False
    interrupt_after: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.join not in ("any", "all"):
            raise GraphError(f"节点 {self.name!r} 的 join 必须是 any 或 all，收到 {self.join!r}")


@dataclass(frozen=True)
class EdgeRule:
    """一条出边。``cond`` 为 None 表示无条件。"""

    src: str
    dst: str
    cond: str | None = None
    label: str = ""


class Graph:
    """声明式构图 + 编译校验。

    路由语义（对单个节点的出边）：
      1. 若节点有 ``router`` 函数，以它的返回值为准；
      2. 否则按声明顺序检查带条件的边，**第一条为真的胜出**；
      3. 都不为真时，所有无条件边**全部**触发（这就是静态 fan-out）。
    """

    def __init__(self, schema: StateSchema, name: str = "workflow") -> None:
        self.schema = schema
        self.name = name
        self.nodes: dict[str, NodeSpec] = {}
        self.rules: list[EdgeRule] = []
        self.entry: str | None = None
        self._compiled = False

    # ---- 构图 ----
    def add_node(self, name: str, fn: AgentFn, **kw: Any) -> Graph:
        if name in self.nodes:
            raise GraphError(f"节点 {name!r} 重复定义")
        if name in (END, START):
            raise GraphError(f"{name!r} 是保留名")
        self.nodes[name] = NodeSpec(name=name, fn=fn, **kw)
        return self

    def add_edge(self, src: str, dst: str, cond: str | None = None, label: str = "") -> Graph:
        self.rules.append(EdgeRule(src=src, dst=dst, cond=cond, label=label))
        return self

    def set_entry(self, name: str) -> Graph:
        self.entry = name
        return self

    # ---- 查询 ----
    def outgoing(self, node: str) -> list[EdgeRule]:
        return [r for r in self.rules if r.src == node]

    def incoming(self, node: str) -> set[str]:
        return {r.src for r in self.rules if r.dst == node}

    def route(self, node: str, state: Mapping[str, Any]) -> list[str]:
        """算出 ``node`` 执行完后的下一跳集合。"""
        spec = self.nodes[node]
        if spec.router is not None:
            out = spec.router(state)
            return [out] if isinstance(out, str) else list(out)
        rules = self.outgoing(node)
        for rule in (r for r in rules if r.cond):
            if safe_eval(rule.cond, state):
                return [rule.dst]
        return [r.dst for r in rules if not r.cond]

    # ---- 编译 ----
    def compile(self) -> Graph:
        """静态校验。宁可在这里炸，也不要跑到第 7 个超步才发现边悬空。"""
        if not self.nodes:
            raise GraphError("图里没有任何节点")
        if self.entry is None:
            raise GraphError("未设置 entry 节点")
        if self.entry not in self.nodes:
            raise GraphError(f"entry 节点 {self.entry!r} 不存在")

        known = set(self.nodes) | {END}
        for rule in self.rules:
            if rule.src not in self.nodes:
                raise GraphError(f"边的源节点 {rule.src!r} 不存在")
            if rule.dst not in known:
                raise GraphError(f"边 {rule.src}->{rule.dst} 的目标节点不存在")
            if rule.cond:
                validate_expr(rule.cond, self.schema.names)

        for name, spec in self.nodes.items():
            if spec.parallel_over and spec.parallel_over not in self.schema.names:
                raise GraphError(
                    f"节点 {name!r} 的 parallel_over={spec.parallel_over!r} 不是已声明的 channel"
                )
            if spec.join == "all" and not self.incoming(name) and name != self.entry:
                raise GraphError(f"节点 {name!r} 声明 join=all 但没有任何入边，会永久阻塞")

        # 可达性：从 entry 出发
        reachable = {self.entry}
        frontier = [self.entry]
        while frontier:
            cur = frontier.pop()
            for rule in self.outgoing(cur):
                if rule.dst != END and rule.dst not in reachable:
                    reachable.add(rule.dst)
                    frontier.append(rule.dst)
        orphans = set(self.nodes) - reachable
        if orphans:
            raise GraphError(f"这些节点从 entry 不可达: {sorted(orphans)}")

        # 能否走到 END：反向可达
        ends = {r.src for r in self.rules if r.dst == END}
        can_end = set(ends)
        changed = True
        while changed:
            changed = False
            for rule in self.rules:
                if rule.dst in can_end and rule.src not in can_end:
                    can_end.add(rule.src)
                    changed = True
        if not ends:
            raise GraphError("没有任何节点连向 END，工作流无法正常终止")
        dead = set(self.nodes) - can_end
        if dead:
            raise GraphError(f"这些节点无法到达 END（会撞 max_steps）: {sorted(dead)}")

        self._compiled = True
        return self

    def to_mermaid(self) -> str:
        """导出 mermaid 流程图，便于人工核对编译结果。"""
        lines = ["flowchart TD", f"  {START}([start]) --> {self.entry}"]
        for rule in self.rules:
            arrow = f"-- {rule.cond} -->" if rule.cond else "-->"
            dst = "__end__([end])" if rule.dst == END else rule.dst
            lines.append(f"  {rule.src} {arrow} {dst}")
        for name, spec in self.nodes.items():
            if spec.parallel_over:
                lines.append(f"  %% {name} fan-out over {spec.parallel_over}")
        return "\n".join(lines)
