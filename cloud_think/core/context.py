"""节点执行上下文：节点跟引擎、存储、工具之间唯一的接触面。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from .errors import Interrupt


@dataclass
class Ctx:
    """传给每个节点的上下文。

    ``state`` 是只读视图——节点想改状态只能通过返回 delta，这是整套可重放语义的根。
    """

    run_id: str
    step: int
    node: str
    instance: str
    state: Mapping[str, Any]
    arg: Any = None                 # fan-out 时分到的那一份输入
    run_dir: Path = field(default_factory=lambda: Path("runs/_scratch"))
    cp: Any = None                  # Checkpointer
    bus: Any = None                 # EventBus
    deps: dict[str, Any] = field(default_factory=dict)   # llm / tools / skill / registry
    usage: dict[str, int] = field(default_factory=lambda: {"tokens_in": 0, "tokens_out": 0})

    def __post_init__(self) -> None:
        if not isinstance(self.state, MappingProxyType):
            self.state = MappingProxyType(dict(self.state))

    # ---- 依赖 ----
    @property
    def llm(self) -> Any:
        return self.deps.get("llm")

    @property
    def tools(self) -> Any:
        return self.deps.get("tools")

    @property
    def skill(self) -> Any:
        return self.deps.get("skill")

    # ---- 事件 ----
    def emit(self, type: str, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.emit(type, run_id=self.run_id, step=self.step, node=self.instance, **payload)

    def add_usage(self, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.usage["tokens_in"] += tokens_in
        self.usage["tokens_out"] += tokens_out

    # ---- 副作用幂等 ----
    async def effect(self, key: str, fn: Callable[[], Any | Awaitable[Any]]) -> Any:
        """执行一次有副作用的操作，重放时直接复用已记录的结果。

        崩溃恢复会重跑整个超步，纯计算重跑无所谓，但写文件、发请求不能重复。
        effect key 带上 step 和 instance，所以同一节点在不同超步（比如第 2 次修订）
        的同名 effect 是两条独立记录。
        """
        scoped = f"{self.step}:{self.instance}:{key}"
        if self.cp is not None:
            hit, cached = await self.cp.get_effect(self.run_id, scoped)
            if hit:
                self.emit("effect_cached", key=scoped)
                return cached
        result = fn()
        if hasattr(result, "__await__"):
            result = await result
        if self.cp is not None:
            await self.cp.put_effect(self.run_id, scoped, result)
        self.emit("effect_done", key=scoped)
        return result

    # ---- 人工介入 ----
    async def interrupt(self, payload: Any, key: str | None = None) -> Any:
        """暂停工作流等待人工输入。已有答复时直接返回答复，不再暂停。"""
        node_key = key or self.instance
        if self.cp is not None:
            answered, answer = await self.cp.get_interrupt_answer(self.run_id, self.step, node_key)
            if answered:
                self.emit("interrupt_resolved", key=node_key, answer=answer)
                return answer
            await self.cp.put_interrupt(self.run_id, self.step, node_key, payload)
        self.emit("interrupt", key=node_key, payload=payload)
        raise Interrupt({"node": self.node, "instance": self.instance,
                         "key": node_key, "step": self.step, "payload": payload})

    # ---- 产物外置 ----
    @property
    def artifacts_dir(self) -> Path:
        d = self.run_dir / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def artifact_path(self, name: str) -> Path:
        return self.artifacts_dir / name

    async def write_artifact(self, name: str, content: str) -> str:
        """把长文写到磁盘，返回一个可放进 state 的引用。

        state 里存引用不存全文——这是主上下文不随任务规模线性膨胀的关键。
        """
        async def _write() -> str:
            path = self.artifact_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return str(path.relative_to(self.run_dir))
        ref = await self.effect(f"artifact:{name}", _write)
        self.emit("artifact_written", name=name, ref=ref, chars=len(content))
        return ref

    def read_artifact(self, ref: str) -> str:
        path = self.run_dir / ref
        if not path.exists():
            path = Path(ref)
        return path.read_text(encoding="utf-8")

    def state_slice(self, keys: list[str] | None) -> dict[str, Any]:
        """取出 state 的一个子集。给 subagent 注入上下文时只给它该看的那部分。"""
        if keys is None:
            return dict(self.state)
        return {k: self.state[k] for k in keys if k in self.state}

    def state_json(self, keys: list[str] | None = None, max_chars: int = 8000) -> str:
        data = self.state_slice(keys)
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…（已截断，原长 {len(text)} 字符）"
        return text
