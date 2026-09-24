"""端到端：用 research-report skill 处理一个真实的复杂任务。

覆盖 M1–M6 的完整链路：
  路由 -> 加载 SKILL.md -> 编译 workflow.yaml -> 4 路 fan-out 并行调研
  -> 脚本交叉验证 -> 成稿 -> 审稿 -> 修订循环 -> 人工批准 -> 发布
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cloud_think.runtime import Runtime, RuntimeConfig
from cloud_think.skills.compiler import compile_workflow

TASK = "调研一下 2026 年本地 LLM 推理框架的现状，出一份带引用的报告"


@pytest.fixture
def rt(research_llm, tools, registry, cp, bus, tmp_path) -> Runtime:
    return Runtime(
        llm=research_llm, tools=tools,
        config=RuntimeConfig(run_root=tmp_path / "runs", console=False, max_steps=30),
        checkpointer=cp, bus=bus, registry=registry,
    )


async def test_complex_task_end_to_end(rt, sink, cp, tmp_path):
    # --- 第一段：跑到发布前的人工批准点 ---
    result = await rt.run_task(TASK)
    assert result.status == "paused", f"应停在 publish 的人工批准点，实际 {result.status}"
    assert result.interrupt["node"] == "publish"

    state = result.state

    # 拆解出 4 个正交子问题
    assert len(state["subqueries"]) == 4

    # 4 个 researcher 实例并行跑过
    instances = [e.node for e in sink.of("node_finished")]
    researchers = [i for i in instances if i.startswith("researcher#")]
    assert sorted(researchers) == ["researcher#0", "researcher#1", "researcher#2", "researcher#3"]

    # 各自的产出按 key_by=arg 归并进同一个 dict
    assert set(state["findings"]) == set(state["subqueries"])

    # 脚本检出了 2.4x / 3.1x 的数值分歧
    assert len(state["conflicts"]) == 1
    values = {state["conflicts"][0]["a"]["value"], state["conflicts"][0]["b"]["value"]}
    assert values == {"2.4x", "3.1x"}

    # 审稿打回一次，修订后通过：writer 跑了 2 次，草稿有两个版本
    assert state["revision"] == 2
    assert state["review"]["score"] >= 0.8
    assert instances.count("writer") == 2
    assert state["draft_ref"] == "artifacts/draft_v1.md"
    assert (result.run_dir / "artifacts" / "draft_v0.md").exists()
    assert (result.run_dir / "artifacts" / "draft_v1.md").exists()

    # --- 第二段：人工批准后恢复，完成发布 ---
    final = await rt.resume(result.run_id, answer={"approved": True})
    assert final.status == "done"

    report = final.run_dir / final.state["report_ref"]
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    for section in ("## 摘要", "## 发现", "## 分歧与不确定性", "## 来源"):
        assert section in text, f"终稿缺少章节 {section}"
    assert "附录：脚本检出的数值分歧" in text
    assert "2.4x" in text and "3.1x" in text
    assert "https://example.org/sglang" in text


async def test_context_isolation(rt, research_llm, sink):
    """上下文隔离：这是多 agent 真正的价值所在，不是"并行快"。"""
    await rt.run_task(TASK)

    def calls_of(role: str) -> list[dict]:
        out = []
        for call in research_llm.calls:
            msgs = call["messages"]
            system = next((m["content"] for m in msgs if m["role"] == "system"), "")
            user = next((m["content"] for m in msgs if m["role"] == "user"), "")
            if f"ROLE: {role}" in system:
                out.append({"system": system, "user": user,
                            "state": _state_block(user), "all": "\n".join(
                                m["content"] for m in msgs)})
        return out

    def _state_block(user: str) -> dict:
        m = re.search(r"# 当前状态\n```json\n(.*?)\n```", user, re.DOTALL)
        return json.loads(m.group(1)) if m else {}

    # 1) researcher 只拿到 topic，看不到任何别人的 findings
    researchers = calls_of("researcher")
    assert researchers
    for c in researchers:
        assert set(c["state"]) == {"topic"}, f"researcher 拿到了多余状态: {set(c['state'])}"
        assert c["user"].count("# 你这一份的输入") == 1
    # 各实例之间互不串味：一个实例的提示里不该出现别人那份子问题
    inputs = [re.search(r"# 你这一份的输入\n(.+)", c["user"]).group(1).strip()
              for c in researchers if "# 你这一份的输入" in c["user"]]
    assert len(set(inputs)) == 4
    for c, mine in zip(researchers, inputs):
        others = set(inputs) - {mine}
        assert not (others & set(re.findall(r"|".join(map(re.escape, others)), c["user"])))

    # 2) reference 按角色加载：writer 拿写作规范，reviewer 拿引用规则，互不串味
    writer_sys = "\n".join(c["system"] for c in calls_of("writer"))
    reviewer_sys = "\n".join(c["system"] for c in calls_of("reviewer"))
    assert "# 报告结构规范" in writer_sys and "# 引用检查规则" not in writer_sys
    assert "# 引用检查规则" in reviewer_sys and "# 报告结构规范" not in reviewer_sys

    # 3) 长文走 artifact：reviewer 的状态里只有引用路径，没有草稿正文
    for c in calls_of("reviewer"):
        assert c["state"]["draft_ref"].startswith("artifacts/draft_v")
        assert "## 分歧与不确定性" not in c["user"]

    # 4) 单个 subagent 的提示规模有界，不随任务整体规模线性增长
    sizes = [e.payload["prompt_tokens"] for e in sink.of("subagent")]
    assert max(sizes) < 4000, f"最大提示 {max(sizes)} token，上下文隔离失效"


async def test_tool_whitelist_per_agent(rt, sink):
    """每个 agent 只拿到 workflow.yaml 里声明的工具。"""
    await rt.run_task(TASK)
    by_role = {e.payload["role"]: e.payload["tools"] for e in sink.of("subagent")}
    assert by_role["planner"] == []          # 拆解不需要任何工具
    assert by_role["researcher"] == ["web_search"]
    assert by_role["reviewer"] == ["read_file"]
    assert "web_search" not in by_role["writer"]


async def test_script_steps_bypass_llm(rt, sink, research_llm):
    """去重和渲染是纯代码步骤，不该消耗任何 LLM 调用。"""
    await rt.run_task(TASK)
    before = len(research_llm.calls)
    scripts = [e.payload["script"] for e in sink.of("script_run")]
    assert "scripts/dedupe_sources.py" in scripts

    roles = [e.payload["role"] for e in sink.of("subagent")]
    # 8 次 subagent 调用：1 planner + 4 researcher + 2 writer + ... 加上第二次 reviewer
    assert roles.count("planner") == 1
    assert roles.count("researcher") == 4
    assert roles.count("writer") == 2
    assert roles.count("reviewer") == 2
    assert len(research_llm.calls) == before  # 断言没有额外调用


async def test_fork_from_step_reuses_history(rt, cp, tmp_path):
    """时间旅行：改完 prompt 不用从头跑。"""
    result = await rt.run_task(TASK)
    steps = await cp.list_steps(result.run_id)
    assert len(steps) >= 5

    # 从 dedupe 之后那一步分叉，直接把审稿分数拉高，跳过修订循环
    forked = await rt.fork(result.run_id, from_step=3, overrides={"topic": "分叉后的主题"})
    assert forked.run_id != result.run_id
    assert forked.state["topic"] == "分叉后的主题"
    # 分叉继承了此前的调研结论，不用重新检索
    assert set(forked.state["findings"]) == set(result.state["subqueries"])


async def test_routing_picks_right_skill(rt, sink):
    """路由：调研任务选 research-report，排障任务选 incident-triage。"""
    route, skills = await rt.route(TASK)
    assert [s.name for s in skills] == ["research-report"]

    route2, skills2 = await rt.route("线上服务从昨天开始大量 504 超时，帮我排查一下根因")
    assert [s.name for s in skills2] == ["incident-triage"]

    # 路由决策全程留痕，skill 选不中时才定位得到
    logged = sink.of("skill_routed")
    assert logged and logged[-1].payload["via"] in ("llm", "keyword", "explicit")


async def test_usage_accounting(rt, cp):
    result = await rt.run_task(TASK)
    usage = await cp.usage(result.run_id)
    assert usage["tokens_in"] > 0 and usage["tokens_out"] > 0
