"""core 层：状态合并、表达式、图校验、超步调度、重试、失败路由。"""
from __future__ import annotations

import asyncio

import pytest

from cloud_think.core.checkpoint import MemoryCheckpointer
from cloud_think.core.engine import Engine
from cloud_think.core.errors import (ConflictError, ExpressionError, GraphError,
                                     MaxStepsExceeded, MaxVisitsExceeded, NodeFailed)
from cloud_think.core.events import EventBus, MemorySink
from cloud_think.core.expr import safe_eval, validate_expr
from cloud_think.core.graph import END, Graph
from cloud_think.core.retry import RetryPolicy
from cloud_think.core.state import StateSchema


async def noop(ctx):
    return {}


def build(schema_spec, nodes, edges, entry):
    schema = StateSchema.from_spec(schema_spec)
    g = Graph(schema, "t")
    for name, fn, kw in nodes:
        g.add_node(name, fn, **kw)
    g.set_entry(entry)
    for src, dst, cond in edges:
        g.add_edge(src, dst, cond=cond)
    return g.compile()


# ---------------------------------------------------------------- 状态
class TestState:
    def test_reducers(self):
        s = StateSchema.from_spec({
            "a": {"type": "int", "reducer": "add"},
            "b": {"type": "dict", "reducer": "merge"},
            "c": {"type": "list", "reducer": "extend"},
            "d": {"type": "list", "reducer": "union"},
        })
        st = s.initial()
        st, _ = s.apply(st, [("x", {"a": 2, "b": {"k": 1}, "c": [1], "d": [1, 2]}),
                             ("y", {"a": 3, "b": {"j": 2}, "c": [2], "d": [2, 3]})])
        assert st == {"a": 5, "b": {"k": 1, "j": 2}, "c": [1, 2], "d": [1, 2, 3]}

    def test_exclusive_channel_conflict(self):
        s = StateSchema.from_spec({"t": "str"})
        with pytest.raises(ConflictError) as e:
            s.apply(s.initial(), [("a", {"t": "p"}), ("b", {"t": "q"})])
        assert "显式声明 reducer" in str(e.value)

    def test_single_write_to_exclusive_is_fine(self):
        s = StateSchema.from_spec({"t": "str"})
        st, _ = s.apply(s.initial(), [("a", {"t": "p"})])
        assert st["t"] == "p"

    def test_unknown_channel_rejected(self):
        s = StateSchema.from_spec({"t": "str"})
        with pytest.raises(KeyError):
            s.apply(s.initial(), [("a", {"nope": 1})])

    def test_defaults_by_type(self):
        s = StateSchema.from_spec({"a": "str", "b": "int", "c": "list",
                                   "d": "dict", "e": {"type": "int", "default": 7}})
        assert s.initial() == {"a": "", "b": 0, "c": [], "d": {}, "e": 7}


# ---------------------------------------------------------------- 表达式
class TestExpr:
    def test_dotted_and_bool(self):
        st = {"review": {"score": 0.72}, "revision": 1}
        assert safe_eval("review.score < 0.8 and revision < 3", st) is True
        assert safe_eval("review.score >= 0.8", st) is False

    def test_none_comparison_is_false_not_crash(self):
        assert safe_eval("r.score < 0.8", {"r": None}) is False

    @pytest.mark.parametrize("expr", [
        "__import__('os').system('ls')", "open('/etc/passwd').read()",
        "[x for x in range(3)]", "(lambda: 1)()",
    ])
    def test_blocks_dangerous(self, expr):
        with pytest.raises(ExpressionError):
            safe_eval(expr, {})

    def test_validate_catches_unknown_channel(self):
        with pytest.raises(ExpressionError):
            validate_expr("nope > 1", {"known"})


# ---------------------------------------------------------------- 图
class TestGraph:
    def test_orphan_node_rejected(self):
        with pytest.raises(GraphError, match="不可达"):
            build({"a": "str"}, [("x", noop, {}), ("orphan", noop, {})],
                  [("x", END, None)], "x")

    def test_node_that_cannot_reach_end_rejected(self):
        # y 是死胡同：进得去出不来，跑到它就只能撞 max_steps
        with pytest.raises(GraphError, match="无法到达 END"):
            build({"a": "str"}, [("x", noop, {}), ("y", noop, {})],
                  [("x", "y", None), ("x", END, None)], "x")

    def test_cycle_that_can_still_reach_end_is_allowed(self):
        # 反思循环是合法的：只要存在通往 END 的路径
        g = build({"a": "str"}, [("x", noop, {}), ("y", noop, {})],
                  [("x", "y", None), ("y", "x", None), ("x", END, None)], "x")
        assert set(g.nodes) == {"x", "y"}

    def test_dangling_edge_rejected(self):
        with pytest.raises(GraphError, match="目标节点不存在"):
            build({"a": "str"}, [("x", noop, {})], [("x", "ghost", None)], "x")

    def test_bad_condition_rejected_at_compile(self):
        with pytest.raises(ExpressionError):
            build({"a": "str"}, [("x", noop, {})],
                  [("x", END, "undeclared > 1")], "x")

    def test_parallel_over_unknown_channel_rejected(self):
        with pytest.raises(GraphError, match="parallel_over"):
            build({"a": "str"}, [("x", noop, {"parallel_over": "ghost"})],
                  [("x", END, None)], "x")

    def test_conditional_first_match_wins(self):
        g = build({"n": "int"}, [("x", noop, {})],
                  [("x", "x", "n < 2"), ("x", END, None)], "x")
        assert g.route("x", {"n": 1}) == ["x"]
        assert g.route("x", {"n": 5}) == [END]


# ---------------------------------------------------------------- 引擎
class TestEngine:
    async def test_fanout_and_join_all(self):
        async def plan(ctx):
            return {"items": ["a", "b", "c"]}

        async def work(ctx):
            return {"out": {ctx.arg: 1}}

        async def gather(ctx):
            return {"total": len(ctx.state["out"])}

        g = build({"items": {"type": "list"}, "out": {"type": "dict", "reducer": "merge"},
                   "total": "int"},
                  [("plan", plan, {}), ("work", work, {"parallel_over": "items"}),
                   ("gather", gather, {"join": "all"})],
                  [("plan", "work", None), ("work", "gather", None), ("gather", END, None)],
                  "plan")
        sink = MemorySink()
        eng = Engine(g, MemoryCheckpointer(), EventBus([sink]), run_root="/tmp/ct-unit")
        r = await eng.invoke()
        assert r.state["total"] == 3
        assert len([e for e in sink.of("node_finished") if e.node.startswith("work#")]) == 3

    async def test_empty_fanout_does_not_stall(self):
        async def plan(ctx):
            return {"items": []}

        async def work(ctx):
            return {"total": 1}

        async def after(ctx):
            return {"total": ctx.state["total"] + 100}

        g = build({"items": {"type": "list"}, "total": "int"},
                  [("plan", plan, {}), ("work", work, {"parallel_over": "items"}),
                   ("after", after, {})],
                  [("plan", "work", None), ("work", "after", None), ("after", END, None)],
                  "plan")
        sink = MemorySink()
        eng = Engine(g, MemoryCheckpointer(), EventBus([sink]), run_root="/tmp/ct-unit")
        r = await eng.invoke()
        assert r.status == "done"
        assert r.state["total"] == 100          # work 没跑，但链路没断
        assert sink.of("fanout_empty")

    async def test_retry_then_succeed(self):
        attempts = {"n": 0}

        async def flaky(ctx):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("boom")
            return {"v": attempts["n"]}

        g = build({"v": "int"},
                  [("f", flaky, {"retry": RetryPolicy(max_attempts=3, base=0.01)})],
                  [("f", END, None)], "f")
        sink = MemorySink()
        eng = Engine(g, MemoryCheckpointer(), EventBus([sink]), run_root="/tmp/ct-unit")
        r = await eng.invoke()
        assert r.state["v"] == 3
        assert len(sink.of("node_retry")) == 2

    async def test_give_up_on_non_retryable(self):
        attempts = {"n": 0}

        async def bad(ctx):
            attempts["n"] += 1
            raise ValueError("参数不合法，重试多少次都一样")

        g = build({"v": "int"},
                  [("f", bad, {"retry": RetryPolicy(max_attempts=5, base=0.01,
                                                    give_up_on=(ValueError,))})],
                  [("f", END, None)], "f")
        eng = Engine(g, MemoryCheckpointer(), EventBus(), run_root="/tmp/ct-unit")
        with pytest.raises(NodeFailed):
            await eng.invoke()
        assert attempts["n"] == 1

    async def test_timeout(self):
        async def slow(ctx):
            await asyncio.sleep(5)
            return {}

        g = build({"v": "int"}, [("f", slow, {"timeout": 0.05})], [("f", END, None)], "f")
        eng = Engine(g, MemoryCheckpointer(), EventBus(), run_root="/tmp/ct-unit")
        with pytest.raises(NodeFailed):
            await eng.invoke()

    async def test_on_error_routes_instead_of_failing(self):
        async def bad(ctx):
            raise RuntimeError("挂了")

        async def fallback(ctx):
            return {"v": -1}

        g = build({"v": "int"},
                  [("f", bad, {"retry": RetryPolicy(max_attempts=1),
                               "meta": {"on_error": "fb"}}),
                   ("fb", fallback, {})],
                  [("f", "fb", None), ("fb", END, None)], "f")
        eng = Engine(g, MemoryCheckpointer(), EventBus(), run_root="/tmp/ct-unit")
        r = await eng.invoke()
        assert r.status == "done" and r.state["v"] == -1

    async def test_max_steps_guard(self):
        async def loop(ctx):
            return {"n": 1}

        g = build({"n": {"type": "int", "reducer": "add"}},
                  [("x", loop, {"max_visits": 999})],
                  [("x", "x", "n < 100"), ("x", END, None)], "x")
        eng = Engine(g, MemoryCheckpointer(), EventBus(), run_root="/tmp/ct-unit", max_steps=5)
        with pytest.raises(MaxStepsExceeded):
            await eng.invoke()

    async def test_max_visits_guard(self):
        async def loop(ctx):
            return {"n": 1}

        g = build({"n": {"type": "int", "reducer": "add"}},
                  [("x", loop, {"max_visits": 3})],
                  [("x", "x", "n < 100"), ("x", END, None)], "x")
        eng = Engine(g, MemoryCheckpointer(), EventBus(), run_root="/tmp/ct-unit", max_steps=50)
        with pytest.raises(MaxVisitsExceeded):
            await eng.invoke()

    async def test_concurrent_write_to_exclusive_channel_is_caught(self):
        async def plan(ctx):
            return {"items": ["a", "b"]}

        async def work(ctx):
            return {"shared": ctx.arg}       # 两个实例同时写 replace channel

        g = build({"items": {"type": "list"}, "shared": "str"},
                  [("plan", plan, {}), ("work", work, {"parallel_over": "items"})],
                  [("plan", "work", None), ("work", END, None)], "plan")
        eng = Engine(g, MemoryCheckpointer(), EventBus(), run_root="/tmp/ct-unit")
        with pytest.raises(ConflictError):
            await eng.invoke()


class TestEventBus:
    def test_same_sink_added_twice_is_deduped(self, tmp_path):
        """装配层和调用方各 add 一遍同一个 sink 是常见写法，
        不去重的话每条事件写两遍，trace 就废了。"""
        from cloud_think.core.checkpoint import SQLiteCheckpointer
        from cloud_think.core.events import JsonlSink

        sink = MemorySink()
        bus = EventBus([sink, sink])
        bus.add(sink)
        bus.emit("x", run_id="r")
        assert len(sink.events) == 1

        cp = SQLiteCheckpointer(tmp_path / "a.db")
        bus2 = EventBus([cp])
        bus2.add(SQLiteCheckpointer(tmp_path / "a.db"))   # 同一个库的另一个实例
        assert len(bus2.sinks) == 1

        bus3 = EventBus([JsonlSink(tmp_path / "t.jsonl")])
        bus3.add(JsonlSink(tmp_path / "t.jsonl"))
        assert len(bus3.sinks) == 1

    def test_sink_failure_does_not_break_run(self, capsys):
        class Broken:
            def handle(self, event):
                raise RuntimeError("sink 挂了")

        good = MemorySink()
        bus = EventBus([Broken(), good])
        bus.emit("x", run_id="r")
        assert len(good.events) == 1
        assert "event-sink-error" in capsys.readouterr().err
