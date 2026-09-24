"""工具层、沙箱、SubAgent、agentic 模式。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cloud_think.agents.orchestrator import Orchestrator, build_agentic_graph
from cloud_think.agents.sub_agent import SubAgent, SubAgentSpec
from cloud_think.core.checkpoint import MemoryCheckpointer
from cloud_think.core.context import Ctx
from cloud_think.core.engine import Engine
from cloud_think.core.errors import OutputParseError, SandboxError
from cloud_think.core.events import EventBus, MemorySink
from cloud_think.llm.base import extract_json
from cloud_think.llm.mock import ScriptedLLM, tool_reply
from cloud_think.tools.registry import ToolRegistry
from cloud_think.tools.sandbox import run_script, run_script_json

SCRIPTS = Path(__file__).parent / "scripts"


@pytest.fixture(scope="module", autouse=True)
def _scripts():
    SCRIPTS.mkdir(exist_ok=True)
    (SCRIPTS / "echo.py").write_text(
        "import json,sys,os\n"
        "d=json.load(sys.stdin)\n"
        "print(json.dumps({'got':d,'secret':os.environ.get('CT_TEST_SECRET'),"
        "'cwd':os.getcwd()}))\n", encoding="utf-8")
    (SCRIPTS / "slow.py").write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    (SCRIPTS / "boom.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    (SCRIPTS / "notjson.py").write_text("print('这不是 JSON')\n", encoding="utf-8")
    yield


class TestSandbox:
    async def test_stdin_stdout_contract(self, tmp_path):
        out = await run_script_json(SCRIPTS / "echo.py", {"x": 1}, cwd=tmp_path)
        assert out["got"] == {"x": 1}

    async def test_env_allowlist_blocks_secret_leak(self, tmp_path):
        os.environ["CT_TEST_SECRET"] = "不该泄漏"
        try:
            out = await run_script_json(SCRIPTS / "echo.py", {}, cwd=tmp_path)
            assert out["secret"] is None, "skill 脚本读到了不在白名单里的环境变量"
        finally:
            os.environ.pop("CT_TEST_SECRET", None)

    async def test_cwd_is_locked_to_run_dir(self, tmp_path):
        out = await run_script_json(SCRIPTS / "echo.py", {}, cwd=tmp_path)
        assert Path(out["cwd"]).resolve() == tmp_path.resolve()

    async def test_timeout(self, tmp_path):
        with pytest.raises(SandboxError, match="超时"):
            await run_script(SCRIPTS / "slow.py", {}, cwd=tmp_path, timeout=0.3)

    async def test_nonzero_exit(self, tmp_path):
        with pytest.raises(SandboxError, match="退出码 3"):
            await run_script(SCRIPTS / "boom.py", {}, cwd=tmp_path)

    async def test_non_json_output(self, tmp_path):
        with pytest.raises(SandboxError, match="不是合法 JSON"):
            await run_script_json(SCRIPTS / "notjson.py", {}, cwd=tmp_path)

    async def test_path_escape_blocked(self, tmp_path):
        with pytest.raises(SandboxError, match="超出允许目录"):
            await run_script(SCRIPTS / "echo.py", {}, cwd=tmp_path,
                             allowed_root=tmp_path)


class TestToolRegistry:
    def test_subset_is_minimal_by_default(self, tools):
        assert len(tools.subset(None)) == 0
        assert tools.subset(["web_search"]).names() == ["web_search"]

    def test_unknown_tool_rejected(self, tools):
        with pytest.raises(KeyError, match="未注册的工具"):
            tools.subset(["nope"])

    async def test_file_write_outside_workspace_blocked(self, tools):
        with pytest.raises(PermissionError):
            await tools.get("write_file").call(None, path="/etc/x", content="no")


def make_ctx(llm, tools, tmp_path, state=None, arg=None, skill=None):
    return Ctx(run_id="r1", step=0, node="n", instance="n", state=state or {},
               arg=arg, run_dir=tmp_path, cp=MemoryCheckpointer(), bus=EventBus(),
               deps={"llm": llm, "tools": tools, "skill": skill})


class TestSubAgent:
    async def test_only_declared_state_is_injected(self, tools, tmp_path):
        seen = {}

        def capture(msgs):
            seen["user"] = msgs[-1].content
            return {"out": 1}

        llm = ScriptedLLM(default=capture)
        spec = SubAgentSpec(role="r", state_slice=["a"], output={"out": "json"})
        ctx = make_ctx(llm, tools, tmp_path, state={"a": 1, "secret": "不该出现"})
        await SubAgent(spec).run(ctx)
        assert '"a": 1' in seen["user"] and "不该出现" not in seen["user"]

    async def test_no_state_slice_means_no_state(self, tools, tmp_path):
        seen = {}
        llm = ScriptedLLM(default=lambda m: (seen.update(user=m[-1].content), {"out": 1})[1])
        ctx = make_ctx(llm, tools, tmp_path, state={"a": 1})
        await SubAgent(SubAgentSpec(role="r", output={"out": "json"})).run(ctx)
        assert "# 当前状态" not in seen["user"]

    async def test_tool_whitelist_enforced(self, tools, tmp_path):
        seen = {}
        llm = ScriptedLLM(default=lambda m: {"out": 1})

        async def run_with(names):
            ctx = make_ctx(llm, tools, tmp_path)
            spec = SubAgentSpec(role="r", tools=names, output={"out": "json"})
            await SubAgent(spec).run(ctx)
            return llm.calls[-1]["tools"]

        assert await run_with(["web_search"]) == ["web_search"]
        assert await run_with([]) is None or await run_with([]) == []

    async def test_artifact_output_stores_reference_not_content(self, tools, tmp_path):
        long_text = "正文" * 3000
        llm = ScriptedLLM(default=long_text)
        spec = SubAgentSpec(role="w", output={"draft": "artifact"},
                            artifact_name="d_{step}.md")
        ctx = make_ctx(llm, tools, tmp_path)
        delta = await SubAgent(spec).run(ctx)
        assert delta["draft"] == "artifacts/d_0.md"
        assert len(delta["draft"]) < 50, "state 里存的应是引用不是正文"
        assert (tmp_path / "artifacts" / "d_0.md").read_text(encoding="utf-8") == long_text

    async def test_key_by_arg_merges_fanout_results(self, tools, tmp_path):
        llm = ScriptedLLM(default={"claims": ["c1"]})
        spec = SubAgentSpec(role="r", output={"findings": "json"}, key_by="arg")
        ctx = make_ctx(llm, tools, tmp_path, arg="子问题A")
        delta = await SubAgent(spec).run(ctx)
        assert delta == {"findings": {"子问题A": {"claims": ["c1"]}}}

    async def test_const_delta_applied(self, tools, tmp_path):
        llm = ScriptedLLM(default="正文")
        spec = SubAgentSpec(role="w", output={"draft": "text"}, const={"revision": 1})
        delta = await SubAgent(spec).run(make_ctx(llm, tools, tmp_path))
        assert delta == {"draft": "正文", "revision": 1}

    async def test_tool_loop_runs_then_returns_json(self, tools, tmp_path, corpus):
        calls = {"n": 0}

        def reply(msgs):
            calls["n"] += 1
            if not any(m.role == "tool" for m in msgs):
                return tool_reply("web_search", {"query": "量化"})
            return {"out": len([m for m in msgs if m.role == "tool"])}

        llm = ScriptedLLM(default=reply)
        spec = SubAgentSpec(role="r", tools=["web_search"], output={"out": "json"})
        delta = await SubAgent(spec).run(make_ctx(llm, tools, tmp_path))
        assert delta == {"out": 1} and calls["n"] == 2

    async def test_tool_error_is_fed_back_not_fatal(self, tools, tmp_path):
        def reply(msgs):
            if not any(m.role == "tool" for m in msgs):
                return tool_reply("read_file", {"path": "不存在的文件.txt"})
            return {"out": "recovered"}

        llm = ScriptedLLM(default=reply)
        sink = MemorySink()
        ctx = make_ctx(llm, tools, tmp_path)
        ctx.bus = EventBus([sink])
        spec = SubAgentSpec(role="r", tools=["read_file"], output={"out": "json"})
        delta = await SubAgent(spec).run(ctx)
        assert delta == {"out": "recovered"}
        assert sink.of("tool_call")[0].payload["ok"] is False

    async def test_bad_json_output_raises(self, tools, tmp_path):
        llm = ScriptedLLM(default="模型忘了输出 JSON")
        spec = SubAgentSpec(role="r", output={"out": "json"})
        with pytest.raises(OutputParseError):
            await SubAgent(spec).run(make_ctx(llm, tools, tmp_path))

    async def test_context_ref_loaded_only_when_declared(self, tools, tmp_path, registry):
        skill = registry.get("research-report")
        seen = {}
        llm = ScriptedLLM(default=lambda m: (seen.update(sys=m[0].content), {"o": 1})[1])
        ctx = make_ctx(llm, tools, tmp_path, skill=skill)
        await SubAgent(SubAgentSpec(role="r", context_refs=["references/style-guide.md"],
                                    output={"o": "json"}), skill).run(ctx)
        assert "# 报告结构规范" in seen["sys"]
        assert "# 引用检查规则" not in seen["sys"]


class TestAgenticMode:
    async def test_orchestrator_spawns_subagents_and_finishes(self, tools, tmp_path,
                                                              registry):
        """模式 A：orchestrator 读 SKILL.md 自行分派，子 agent 上下文独立。"""
        skill = registry.get("incident-triage")
        phase = {"n": 0}

        def reply(msgs):
            text = "\n".join(m.content for m in msgs)
            if "ROLE: log-digger" in text:
                return "日志显示 upstream 连接池在 03:14 耗尽，见 conf/pool.yaml:12"
            if "ROLE: change-auditor" in text:
                return "最近变更：commit a1b2c3 把 pool_size 从 50 改成 5"
            phase["n"] += 1
            if phase["n"] == 1:
                return tool_reply("spawn_subagent",
                                  {"role": "log-digger", "task": "查日志定位耗尽时间点"}, "t1")
            if phase["n"] == 2:
                return tool_reply("spawn_subagent",
                                  {"role": "change-auditor", "task": "查最近变更"}, "t2")
            return tool_reply("finish", {"result": "根因：pool_size 被改小",
                                         "notes": ["回滚 a1b2c3"]}, "t3")

        llm = ScriptedLLM(default=reply)
        graph = build_agentic_graph(skill)
        eng = Engine(graph, MemoryCheckpointer(), EventBus(),
                     run_root=tmp_path / "runs",
                     deps={"llm": llm, "tools": tools, "skill": skill})
        r = await eng.invoke({"task": "接口大量 504"})

        assert r.status == "done"
        assert r.state["result"] == "根因：pool_size 被改小"
        assert r.state["notes"] == ["回滚 a1b2c3"]

        # 子 agent 的取证细节留在它自己的上下文里，没有污染 orchestrator
        orch_prompts = [c for c in llm.calls
                        if "你是一个编排者" in "\n".join(m["content"] for m in c["messages"])]
        first = "\n".join(m["content"] for m in orch_prompts[0]["messages"])
        assert "conf/pool.yaml" not in first

    async def test_orchestrator_without_finish_still_returns(self, tools, tmp_path,
                                                             registry):
        skill = registry.get("incident-triage")
        llm = ScriptedLLM(default="我直接给结论了，忘了调 finish")
        sink = MemorySink()
        graph = build_agentic_graph(skill)
        eng = Engine(graph, MemoryCheckpointer(), EventBus([sink]),
                     run_root=tmp_path / "runs",
                     deps={"llm": llm, "tools": tools, "skill": skill})
        r = await eng.invoke({"task": "x"})
        assert r.status == "done" and "忘了调 finish" in r.state["result"]
        assert sink.of("orchestrator_no_finish")
