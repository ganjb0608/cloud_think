"""持久化与恢复：崩溃重启、副作用幂等、人工介入、时间旅行。

这些是"任务跑到十几分钟量级"之后真正值钱的能力，也是最容易写出隐蔽 bug 的地方。
"""
from __future__ import annotations

import asyncio

import pytest

from cloud_think.core.checkpoint import (Checkpoint, MemoryCheckpointer,
                                         SQLiteCheckpointer, TaskRef, new_run_id)
from cloud_think.core.engine import Engine
from cloud_think.core.errors import NodeFailed
from cloud_think.core.events import EventBus, MemorySink
from cloud_think.core.graph import END, Graph
from cloud_think.core.retry import RetryPolicy
from cloud_think.core.state import StateSchema


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """同一套断言在内存实现和 SQLite 实现上都要成立。"""
    if request.param == "memory":
        return MemoryCheckpointer()
    return SQLiteCheckpointer(tmp_path / "t.db")


class TestCheckpointStore:
    async def test_roundtrip(self, store):
        rid = new_run_id()
        await store.create_run(rid, "wf", {"skill": "s"})
        await store.save(Checkpoint(run_id=rid, step=2, state={"a": [1, 2]},
                                    frontier=[TaskRef("n", arg={"q": "x"}, instance="n#0")],
                                    arrivals={"n": ["m"]}, visits={"n": 1}))
        got = await store.load_latest(rid)
        assert got.step == 2 and got.state == {"a": [1, 2]}
        assert got.frontier[0].arg == {"q": "x"} and got.frontier[0].instance == "n#0"
        assert got.arrivals == {"n": ["m"]} and got.visits == {"n": 1}

    async def test_effect_idempotency(self, store):
        rid = new_run_id()
        await store.create_run(rid, "wf", {})
        assert await store.get_effect(rid, "k") == (False, None)
        await store.put_effect(rid, "k", {"r": 1})
        assert await store.get_effect(rid, "k") == (True, {"r": 1})

    async def test_interrupt_lifecycle(self, store):
        rid = new_run_id()
        await store.create_run(rid, "wf", {})
        await store.put_interrupt(rid, 3, "publish", {"q": "批准吗"})
        assert (await store.pending_interrupt(rid))["payload"] == {"q": "批准吗"}
        assert await store.get_interrupt_answer(rid, 3, "publish") == (False, None)
        await store.answer_interrupt(rid, 3, "publish", {"approved": True})
        assert await store.get_interrupt_answer(rid, 3, "publish") == (True, {"approved": True})
        assert await store.pending_interrupt(rid) is None


# ------------------------------------------------------------------ 崩溃恢复
CALLS: dict[str, int] = {}


def _crash_graph(should_fail):
    """一条 a -> b -> END 的链，b 在开关打开时炸掉。

    b 在炸之前已经做了一次有副作用的操作——恢复后它绝不能重做。
    """
    schema = StateSchema.from_spec({"log": {"type": "list", "reducer": "extend"},
                                    "done": "str"})

    async def a(ctx):
        CALLS["a"] = CALLS.get("a", 0) + 1
        return {"log": ["a"]}

    async def b(ctx):
        CALLS["b"] = CALLS.get("b", 0) + 1

        async def side_effect():
            CALLS["effect"] = CALLS.get("effect", 0) + 1
            return "已写入外部系统"

        note = await ctx.effect("external_write", side_effect)
        if should_fail["v"]:
            raise RuntimeError("模拟进程崩溃")
        return {"log": ["b"], "done": note}

    g = Graph(schema, "crash")
    g.add_node("a", a)
    g.add_node("b", b, retry=RetryPolicy(max_attempts=1))
    g.set_entry("a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g.compile()


class TestCrashResume:
    async def test_resume_does_not_repeat_side_effects(self, store, tmp_path):
        CALLS.clear()
        should_fail = {"v": True}
        graph = _crash_graph(should_fail)
        sink = MemorySink()
        run_id = new_run_id()

        eng = Engine(graph, store, EventBus([sink]), run_root=tmp_path / "runs")
        with pytest.raises(NodeFailed):
            await eng.invoke(run_id=run_id)

        run = await store.get_run(run_id)
        assert run["status"] == "failed"
        assert CALLS["effect"] == 1, "崩溃前副作用执行了一次"

        # 修好之后用**新的引擎实例**恢复，模拟进程重启
        should_fail["v"] = False
        eng2 = Engine(_crash_graph(should_fail), store, EventBus([sink]),
                      run_root=tmp_path / "runs")
        result = await eng2.resume(run_id)

        assert result.status == "done"
        assert result.state["done"] == "已写入外部系统"
        assert CALLS["effect"] == 1, "恢复后副作用被重复执行了"
        assert CALLS["b"] == 2, "b 的纯计算部分重跑（这是对的）"
        assert CALLS["a"] == 1, "已完成的超步不该重跑"
        assert result.state["log"] == ["a", "b"], "日志不该出现重复的 a"

    async def test_checkpoint_written_per_superstep(self, store, tmp_path):
        CALLS.clear()
        graph = _crash_graph({"v": False})
        eng = Engine(graph, store, EventBus(), run_root=tmp_path / "runs")
        r = await eng.invoke()
        steps = await store.list_steps(r.run_id)
        assert steps == [0, 1, 2], "每个超步结束都应落一个 checkpoint"
        first = await store.load_at(r.run_id, 0)
        assert first.state["log"] == [], "step 0 保存的是执行前的状态"


# ------------------------------------------------------------------ 人工介入
class TestInterrupt:
    def _graph(self, record):
        schema = StateSchema.from_spec({"draft": "str", "approved": "str", "n":
                                        {"type": "int", "reducer": "add"}})

        async def write(ctx):
            return {"draft": "稿件", "n": 1}

        async def publish(ctx):
            record.append("publish-body-ran")
            answer = await ctx.interrupt({"question": "批准发布吗", "draft": ctx.state["draft"]})
            return {"approved": str(answer)}

        g = Graph(schema, "hitl")
        g.add_node("write", write)
        g.add_node("publish", publish)
        g.set_entry("write")
        g.add_edge("write", "publish")
        g.add_edge("publish", END)
        return g.compile()

    async def test_pause_and_resume_with_answer(self, store, tmp_path):
        record: list[str] = []
        eng = Engine(self._graph(record), store, EventBus(), run_root=tmp_path / "runs")
        r = await eng.invoke()

        assert r.status == "paused"
        assert r.interrupt["node"] == "publish"
        assert r.interrupt["payload"]["question"] == "批准发布吗"
        assert (await store.get_run(r.run_id))["status"] == "paused"

        r2 = await eng.resume(r.run_id, answer="同意")
        assert r2.status == "done"
        assert r2.state["approved"] == "同意"
        assert r2.state["n"] == 1, "上游节点不该因为恢复而重跑"
        assert record == ["publish-body-ran", "publish-body-ran"], "被中断的节点会重放"

    async def test_interrupt_before_gate(self, store, tmp_path):
        schema = StateSchema.from_spec({"v": "int"})
        ran: list[str] = []

        async def gated(ctx):
            ran.append("ran")
            return {"v": 1}

        g = Graph(schema, "gate")
        g.add_node("g", gated, interrupt_before=True)
        g.set_entry("g")
        g.add_edge("g", END)
        g.compile()

        eng = Engine(g, store, EventBus(), run_root=tmp_path / "runs")
        r = await eng.invoke()
        assert r.status == "paused" and ran == [], "interrupt_before 应在节点体执行前拦住"

        r2 = await eng.resume(r.run_id, answer={"ok": True})
        assert r2.status == "done" and ran == ["ran"]


# ------------------------------------------------------------------ 时间旅行
class TestFork:
    async def test_fork_from_step(self, store, tmp_path):
        schema = StateSchema.from_spec({"seed": "str", "trace": {"type": "list", "reducer": "extend"}})

        async def a(ctx):
            return {"trace": [f"a:{ctx.state['seed']}"]}

        async def b(ctx):
            return {"trace": [f"b:{ctx.state['seed']}"]}

        g = Graph(schema, "fork")
        g.add_node("a", a)
        g.add_node("b", b)
        g.set_entry("a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        g.compile()

        eng = Engine(g, store, EventBus(), run_root=tmp_path / "runs")
        r = await eng.invoke({"seed": "原始"})
        assert r.state["trace"] == ["a:原始", "b:原始"]

        # 从 step 1（a 已完成、b 待执行）分叉，换个 seed 重跑后半段
        f = await eng.fork(r.run_id, from_step=1, overrides={"seed": "分叉"})
        assert f.state["trace"] == ["a:原始", "b:分叉"]
        assert f.run_id != r.run_id
        assert (await store.load_latest(r.run_id)).state["seed"] == "原始", "原 run 不受影响"


class TestConcurrentAccess:
    """跨进程/跨 agent 共用同一个库是预期用法，不能一撞锁就报错。"""

    async def test_busy_timeout_is_set(self, tmp_path):
        cp = SQLiteCheckpointer(tmp_path / "c.db")
        (timeout,) = cp._conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout >= 5000

    async def test_two_connections_same_db(self, tmp_path):
        """第二个进程（这里用第二个连接模拟）能读到第一个写入的 checkpoint。"""
        a = SQLiteCheckpointer(tmp_path / "shared.db")
        rid = new_run_id()
        await a.create_run(rid, "wf", {"skill": "s"})
        await a.save(Checkpoint(run_id=rid, step=1, state={"x": 1},
                                frontier=[TaskRef("n")]))
        b = SQLiteCheckpointer(tmp_path / "shared.db")
        got = await b.load_latest(rid)
        assert got is not None and got.state == {"x": 1}
        assert (await b.get_run(rid))["workflow"] == "wf"
