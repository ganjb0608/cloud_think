"""BSP 超步调度器。

一个超步 = 取 frontier -> 并发执行 -> 合并 delta -> 路由 -> 写 checkpoint。
超步之间是全局屏障，所以 checkpoint 天然一致：不存在"半个状态"，
崩溃后 resume 只需重跑最后一个 checkpoint 的 frontier。
"""
from __future__ import annotations

import asyncio
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .checkpoint import Checkpoint, Checkpointer, TaskRef, new_run_id
from .context import Ctx
from .errors import Interrupt, MaxStepsExceeded, MaxVisitsExceeded, NodeFailed
from .events import EventBus
from .graph import END, Graph


@dataclass
class TaskOutcome:
    instance: str
    node: str
    delta: dict[str, Any] = field(default_factory=dict)
    interrupt: Interrupt | None = None
    error: BaseException | None = None
    attempts: int = 1
    usage: dict[str, int] = field(default_factory=lambda: {"tokens_in": 0, "tokens_out": 0})
    elapsed: float = 0.0


@dataclass
class RunResult:
    run_id: str
    status: str                       # done | paused | failed
    state: dict[str, Any]
    steps: int
    run_dir: Path
    interrupt: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "done"


class Engine:
    """工作流执行器。

    Checkpoint 语义：``Checkpoint(step=N)`` 保存的是**执行超步 N 之前**的状态和
    待执行的 frontier。因此 resume 就是"重跑 checkpoint.frontier"，不需要任何
    额外的进度推断。
    """

    def __init__(
        self,
        graph: Graph,
        checkpointer: Checkpointer,
        bus: EventBus | None = None,
        run_root: str | Path = "runs",
        max_steps: int = 50,
        deps: dict[str, Any] | None = None,
    ) -> None:
        self.graph = graph
        self.schema = graph.schema
        self.cp = checkpointer
        self.bus = bus or EventBus()
        self.run_root = Path(run_root)
        self.max_steps = max_steps
        self.deps = deps or {}

    # ------------------------------------------------------------------ 入口
    async def invoke(
        self, inputs: Mapping[str, Any] | None = None,
        run_id: str | None = None, meta: dict[str, Any] | None = None,
    ) -> RunResult:
        run_id = run_id or new_run_id()
        state = self.schema.initial(inputs)
        await self.cp.create_run(run_id, self.graph.name, dict(meta or {}))
        ckpt = Checkpoint(
            run_id=run_id, step=0, state=state,
            frontier=[TaskRef(node=self.graph.entry)], arrivals={}, visits={},
        )
        await self.cp.save(ckpt)
        self.bus.emit("run_started", run_id=run_id, step=0,
                      workflow=self.graph.name, inputs=dict(inputs or {}))
        return await self._drive(ckpt)

    async def resume(self, run_id: str, answer: Any = None) -> RunResult:
        """从最新 checkpoint 继续。``answer`` 用于回答挂起的 interrupt。"""
        ckpt = await self.cp.load_latest(run_id)
        if ckpt is None:
            raise KeyError(f"run {run_id!r} 没有任何 checkpoint")
        if answer is not None:
            pending = await self.cp.pending_interrupt(run_id)
            if pending is None:
                raise ValueError(f"run {run_id!r} 没有等待中的 interrupt")
            await self.cp.answer_interrupt(run_id, pending["step"], pending["node"], answer)
            self.bus.emit("interrupt_answered", run_id=run_id, step=pending["step"],
                          node=pending["node"], answer=answer)
        await self.cp.set_status(run_id, "running")
        ckpt.status = "running"
        ckpt.interrupt = None
        self.bus.emit("run_resumed", run_id=run_id, step=ckpt.step)
        return await self._drive(ckpt)

    async def fork(
        self, run_id: str, from_step: int,
        overrides: Mapping[str, Any] | None = None, new_run_id_: str | None = None,
    ) -> RunResult:
        """从历史某一步分叉出一个新 run（时间旅行）。

        改完 prompt 不用从头跑——这是调试 agent 工作流最省时间的一个能力。
        """
        src = await self.cp.load_at(run_id, from_step)
        if src is None:
            raise KeyError(f"run {run_id!r} 没有 step {from_step} 的 checkpoint")
        child = new_run_id_ or new_run_id("fork")
        state = dict(src.state)
        state.update(overrides or {})
        await self.cp.create_run(child, self.graph.name,
                                 {"forked_from": run_id, "from_step": from_step})
        ckpt = Checkpoint(
            run_id=child, step=src.step, state=state,
            frontier=[TaskRef.from_dict(t.to_dict()) for t in src.frontier],
            arrivals=dict(src.arrivals), visits=dict(src.visits), parent_step=from_step,
        )
        await self.cp.save(ckpt)
        self.bus.emit("run_forked", run_id=child, step=src.step,
                      parent=run_id, from_step=from_step)
        return await self._drive(ckpt)

    # ------------------------------------------------------------------ 主循环
    async def _drive(self, ckpt: Checkpoint) -> RunResult:
        run_id = ckpt.run_id
        run_dir = self.run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        executed_steps = 0

        while True:
            if not ckpt.frontier:
                await self.cp.set_status(run_id, "done")
                ckpt.status = "done"
                await self.cp.save(ckpt)
                usage = await self.cp.usage(run_id)
                self.bus.emit("run_finished", run_id=run_id, step=ckpt.step,
                              steps=ckpt.step, **usage)
                return RunResult(run_id, "done", dict(ckpt.state), ckpt.step, run_dir)

            if executed_steps >= self.max_steps:
                err = f"超过 max_steps={self.max_steps}，图里可能有停不下来的环"
                await self._fail(run_id, ckpt, err)
                raise MaxStepsExceeded(err)

            tasks, skipped = self._expand(ckpt.frontier, ckpt.state)
            for t in tasks:
                visits = ckpt.visits.get(t.node, 0)
                spec = self.graph.nodes[t.node]
                if visits >= spec.max_visits:
                    err = f"节点 {t.node!r} 访问次数超过 max_visits={spec.max_visits}"
                    await self._fail(run_id, ckpt, err)
                    raise MaxVisitsExceeded(err)

            self.bus.emit("step_started", run_id=run_id, step=ckpt.step,
                          nodes=[t.instance for t in tasks])
            outcomes = await self._run_superstep(ckpt, tasks, run_dir)

            # 1) 任一节点触发 interrupt -> 整个超步暂停，保持 frontier 不变
            interrupted = [o for o in outcomes if o.interrupt is not None]
            if interrupted:
                info = interrupted[0].interrupt.payload  # type: ignore[union-attr]
                ckpt.status = "paused"
                ckpt.interrupt = info if isinstance(info, dict) else {"payload": info}
                await self.cp.save(ckpt)
                await self.cp.set_status(run_id, "paused")
                self.bus.emit("run_paused", run_id=run_id, step=ckpt.step, interrupt=ckpt.interrupt)
                return RunResult(run_id, "paused", dict(ckpt.state), ckpt.step,
                                 run_dir, interrupt=ckpt.interrupt)

            # 2) 节点彻底失败 -> 可选 on_error 路由，否则整个 run 失败（checkpoint 留着）
            failed = [o for o in outcomes if o.error is not None]
            error_routes: dict[str, str] = {}
            for o in failed:
                target = self.graph.nodes[o.node].meta.get("on_error")
                if not target:
                    err = f"节点 {o.instance!r} 失败: {o.error!r}"
                    await self._fail(run_id, ckpt, err)
                    raise NodeFailed(o.instance, o.attempts, o.error)  # type: ignore[arg-type]
                error_routes[o.node] = target

            # 3) 合并 delta
            writes = [(o.instance, o.delta) for o in outcomes if o.error is None]
            new_state, touched = self.schema.apply(ckpt.state, writes)
            if touched:
                self.bus.emit("state_updated", run_id=run_id, step=ckpt.step,
                              channels={k: v for k, v in touched.items()})

            # 4) 路由，算出下个超步的 frontier
            executed_nodes: list[str] = []
            for o in outcomes:
                if o.node not in executed_nodes:
                    executed_nodes.append(o.node)
            for n in skipped:
                if n not in executed_nodes:
                    executed_nodes.append(n)

            arrivals = {k: list(v) for k, v in ckpt.arrivals.items()}
            frontier, arrivals, reached_end = self._route_all(
                executed_nodes, new_state, arrivals, error_routes)

            visits = dict(ckpt.visits)
            for n in executed_nodes:
                visits[n] = visits.get(n, 0) + 1

            ckpt = Checkpoint(
                run_id=run_id, step=ckpt.step + 1, state=new_state,
                frontier=frontier, arrivals=arrivals, visits=visits,
                parent_step=ckpt.step,
            )
            await self.cp.save(ckpt)
            executed_steps += 1
            self.bus.emit("step_finished", run_id=run_id, step=ckpt.step - 1,
                          next=[t.node for t in frontier], reached_end=reached_end)

    async def _fail(self, run_id: str, ckpt: Checkpoint, err: str) -> None:
        ckpt.status = "failed"
        await self.cp.save(ckpt)
        await self.cp.set_status(run_id, "failed", err)
        self.bus.emit("run_failed", run_id=run_id, step=ckpt.step, error=err)

    # ------------------------------------------------------------------ 超步内部
    def _expand(self, frontier: list[TaskRef], state: Mapping[str, Any]) -> tuple[list[TaskRef], list[str]]:
        """把 frontier 展开成实际任务，处理 parallel_over 的 fan-out。

        返回 (任务列表, 因列表为空被跳过的节点名)。被跳过的节点仍然参与路由，
        否则整条链会静默断掉。
        """
        tasks: list[TaskRef] = []
        skipped: list[str] = []
        for t in frontier:
            spec = self.graph.nodes[t.node]
            if spec.parallel_over and t.arg is None:
                items = list(state.get(spec.parallel_over) or [])
                if not items:
                    skipped.append(t.node)
                    self.bus.emit("fanout_empty", node=t.node, channel=spec.parallel_over)
                    continue
                tasks += [TaskRef(node=t.node, arg=item, instance=f"{t.node}#{i}")
                          for i, item in enumerate(items)]
            else:
                tasks.append(t)
        return tasks, skipped

    async def _run_superstep(
        self, ckpt: Checkpoint, tasks: list[TaskRef], run_dir: Path
    ) -> list[TaskOutcome]:
        coros = [self._run_task(ckpt, t, run_dir) for t in tasks]
        return list(await asyncio.gather(*coros))

    async def _run_task(self, ckpt: Checkpoint, task: TaskRef, run_dir: Path) -> TaskOutcome:
        spec = self.graph.nodes[task.node]
        run_id = ckpt.run_id
        started = time.time()
        attempt = 0
        last_exc: BaseException | None = None

        while True:
            attempt += 1
            ctx = Ctx(
                run_id=run_id, step=ckpt.step, node=task.node, instance=task.instance,
                state=ckpt.state, arg=task.arg, run_dir=run_dir,
                cp=self.cp, bus=self.bus, deps=self.deps,
            )
            self.bus.emit("node_started", run_id=run_id, step=ckpt.step,
                          node=task.instance, attempt=attempt)
            try:
                if spec.interrupt_before:
                    await ctx.interrupt(
                        {"kind": "before_node", "node": task.node,
                         "preview": ctx.state_json(max_chars=1500)},
                        key=f"{task.instance}::before")

                if spec.timeout:
                    async with asyncio.timeout(spec.timeout):
                        delta = await spec.fn(ctx)
                else:
                    delta = await spec.fn(ctx)
                delta = dict(delta or {})

                if spec.interrupt_after:
                    approved = await ctx.interrupt(
                        {"kind": "after_node", "node": task.node, "delta": delta},
                        key=f"{task.instance}::after")
                    if isinstance(approved, dict) and approved.get("delta") is not None:
                        delta = approved["delta"]   # 人工可以直接改写节点产出

                elapsed = time.time() - started
                await self.cp.record_node_run(
                    run_id=run_id, step=ckpt.step, instance=task.instance, node=task.node,
                    attempt=attempt, status="ok", output=delta, started_at=started,
                    ended_at=time.time(), tokens_in=ctx.usage["tokens_in"],
                    tokens_out=ctx.usage["tokens_out"])
                self.bus.emit("node_finished", run_id=run_id, step=ckpt.step,
                              node=task.instance, channels=sorted(delta),
                              elapsed=round(elapsed, 3), **ctx.usage)
                return TaskOutcome(task.instance, task.node, delta, attempts=attempt,
                                   usage=dict(ctx.usage), elapsed=elapsed)

            except Interrupt as it:
                await self.cp.record_node_run(
                    run_id=run_id, step=ckpt.step, instance=task.instance, node=task.node,
                    attempt=attempt, status="interrupted", started_at=started, ended_at=time.time())
                return TaskOutcome(task.instance, task.node, interrupt=it, attempts=attempt)

            except (asyncio.TimeoutError, Exception) as e:  # noqa: B014
                last_exc = e
                await self.cp.record_node_run(
                    run_id=run_id, step=ckpt.step, instance=task.instance, node=task.node,
                    attempt=attempt, status="error", error=f"{type(e).__name__}: {e}",
                    started_at=started, ended_at=time.time())
                if spec.retry.should_retry(e, attempt):
                    delay = spec.retry.delay_for(attempt)
                    self.bus.emit("node_retry", run_id=run_id, step=ckpt.step,
                                  node=task.instance, attempt=attempt,
                                  delay=round(delay, 2), error=f"{type(e).__name__}: {e}")
                    await asyncio.sleep(delay)
                    continue
                self.bus.emit("node_failed", run_id=run_id, step=ckpt.step, node=task.instance,
                              attempts=attempt, error=f"{type(e).__name__}: {e}",
                              trace=traceback.format_exc(limit=3))
                return TaskOutcome(task.instance, task.node, error=e, attempts=attempt)

    # ------------------------------------------------------------------ 路由
    def _route_all(
        self, executed: list[str], state: Mapping[str, Any],
        arrivals: dict[str, list[str]], error_routes: dict[str, str],
    ) -> tuple[list[TaskRef], dict[str, list[str]], bool]:
        targets: dict[str, set[str]] = {}
        reached_end = False
        for node in executed:
            dsts = [error_routes[node]] if node in error_routes else self.graph.route(node, state)
            for dst in dsts:
                if dst == END:
                    reached_end = True
                    continue
                targets.setdefault(dst, set()).add(node)

        for dst, srcs in targets.items():
            arrivals[dst] = sorted(set(arrivals.get(dst, [])) | srcs)

        ready: list[str] = []
        for dst in list(arrivals):
            spec = self.graph.nodes[dst]
            needed = self.graph.incoming(dst)
            got = set(arrivals[dst])
            if not got:
                continue
            if spec.join == "all" and not got >= needed:
                self.bus.emit("join_waiting", node=dst,
                              got=sorted(got), need=sorted(needed))
                continue
            ready.append(dst)
            arrivals.pop(dst)

        return [TaskRef(node=d) for d in ready], arrivals, reached_end
