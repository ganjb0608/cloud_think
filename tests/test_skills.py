"""Skill 层：加载、校验、路由、编译。

系统最大的风险不在引擎，在 skill 的写法——所以 lint 和路由的测试要厚。
"""
from __future__ import annotations

import textwrap

import pytest

from cloud_think.core.errors import SkillError
from cloud_think.core.events import EventBus, MemorySink
from cloud_think.llm.mock import ScriptedLLM
from cloud_think.skills.compiler import compile_skill, compile_workflow
from cloud_think.skills.lint import lint_all, lint_skill
from cloud_think.skills.loader import load_skill, parse_frontmatter
from cloud_think.skills.registry import SkillRegistry
from cloud_think.skills.router import SkillRouter
from tests.conftest import SKILLS_DIR


def make_skill(tmp_path, name="demo", front="", body="正文" * 60, extra=None):
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    front = front or textwrap.dedent(f"""\
        name: {name}
        description: 演示用 skill。当用户要求演示时使用，不用于其他场景。
        """)
    (d / "SKILL.md").write_text(f"---\n{front}---\n\n{body}\n", encoding="utf-8")
    for rel, content in (extra or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class TestLoader:
    def test_frontmatter_parsing(self):
        meta, body = parse_frontmatter("---\nname: x\ndescription: y\n---\n\n正文在这")
        assert meta == {"name": "x", "description": "y"} and body == "正文在这"

    def test_missing_frontmatter_rejected(self, tmp_path):
        d = tmp_path / "bad"
        d.mkdir()
        (d / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")
        with pytest.raises(SkillError, match="frontmatter"):
            load_skill(d)

    def test_missing_description_rejected(self, tmp_path):
        d = make_skill(tmp_path, "nodesc", front="name: nodesc\n")
        with pytest.raises(SkillError, match="description"):
            load_skill(d)

    def test_name_must_match_directory(self, tmp_path):
        d = make_skill(tmp_path, "dirname",
                       front="name: othername\ndescription: 当需要时使用这个 skill 做演示。\n")
        with pytest.raises(SkillError, match="与目录名"):
            load_skill(d)

    def test_workflow_mode_requires_yaml(self, tmp_path):
        d = make_skill(tmp_path, "wf",
                       front="name: wf\ndescription: 当需要时使用。\nmode: workflow\n")
        with pytest.raises(SkillError, match="没有 workflow.yaml"):
            load_skill(d)

    def test_resource_path_escape_blocked(self, tmp_path):
        skill = load_skill(make_skill(tmp_path))
        with pytest.raises(SkillError, match="越出"):
            skill.resource_path("../../etc/passwd")

    def test_broken_skill_does_not_break_registry(self, tmp_path, capsys):
        make_skill(tmp_path, "good")
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "SKILL.md").write_text("坏文件", encoding="utf-8")
        reg = SkillRegistry([tmp_path])
        assert reg.names() == ["good"]
        assert "skill-load-error" in capsys.readouterr().err


class TestLint:
    def test_real_skills_are_clean(self, registry):
        findings = lint_all(list(registry))
        errors = [f for f in findings if f.level == "error"]
        assert not errors, f"仓库自带 skill 有错误: {[str(f) for f in errors]}"

    def test_description_without_trigger_warned(self, tmp_path):
        d = make_skill(tmp_path, "vague",
                       front="name: vague\ndescription: 这是一个非常强大的通用工具包合集。\n")
        msgs = [f.message for f in lint_skill(load_skill(d))]
        assert any("触发条件" in m for m in msgs)

    def test_missing_referenced_resource_is_error(self, tmp_path):
        d = make_skill(tmp_path, "ref", body="请阅读 `references/ghost.md` 里的规则。" * 5)
        findings = lint_skill(load_skill(d))
        assert any(f.level == "error" and "ghost.md" in f.message for f in findings)

    def test_broken_script_is_error(self, tmp_path):
        d = make_skill(tmp_path, "bs", extra={"scripts/x.py": "def (:\n"})
        assert any(f.level == "error" and "语法错误" in f.message
                   for f in lint_skill(load_skill(d)))


class TestRouter:
    async def test_explicit_call_wins(self, registry):
        r = SkillRouter(registry, llm=None)
        result = await r.route("/incident-triage 服务挂了")
        assert result.via == "explicit" and result.names == ["incident-triage"]

    async def test_keyword_fallback_when_no_llm(self, registry):
        r = SkillRouter(registry, llm=None)
        result = await r.route("帮我做个技术选型对比的调研报告")
        assert result.via == "keyword" and result.names == ["research-report"]

    async def test_llm_route(self, registry):
        llm = ScriptedLLM(default={"skills": ["incident-triage"], "confidence": 0.9,
                                   "reason": "这是排障"})
        r = SkillRouter(registry, llm=llm)
        result = await r.route("接口大量 504")
        assert result.via == "llm" and result.names == ["incident-triage"]
        assert result.top.reason == "这是排障"

    async def test_low_confidence_returns_nothing(self, registry):
        llm = ScriptedLLM(default={"skills": ["research-report"], "confidence": 0.1,
                                   "reason": "不确定"})
        sink = MemorySink()
        r = SkillRouter(registry, llm=llm, bus=EventBus([sink]))
        result = await r.route("今天天气怎么样")
        assert result.names == []
        assert sink.of("skill_route_low_confidence")

    async def test_broken_llm_degrades_to_keyword(self, registry):
        llm = ScriptedLLM(default="这不是 JSON")
        sink = MemorySink()
        r = SkillRouter(registry, llm=llm, bus=EventBus([sink]))
        result = await r.route("做一份调研报告")
        assert result.via == "keyword" and result.names == ["research-report"]
        assert sink.of("skill_route_degraded")

    async def test_decision_is_logged(self, registry):
        sink = MemorySink()
        r = SkillRouter(registry, llm=None, bus=EventBus([sink]))
        await r.route("调研报告")
        logged = sink.of("skill_routed")[0].payload
        assert "candidates" in logged and logged["candidates"]


class TestCompiler:
    def test_compiles_real_workflow(self, registry):
        g = compile_workflow(registry.get("research-report"))
        assert set(g.nodes) == {"planner", "researcher", "dedupe", "writer",
                                "reviewer", "publish"}
        assert g.nodes["researcher"].parallel_over == "subqueries"
        assert g.nodes["dedupe"].join == "all"
        assert g.nodes["publish"].interrupt_before is True
        assert g.nodes["researcher"].retry.max_attempts == 3

    def test_agentic_skill_compiles_to_single_node(self, registry):
        g = compile_skill(registry.get("incident-triage"))
        assert set(g.nodes) == {"orchestrator"}

    def test_undeclared_output_channel_rejected(self, tmp_path):
        wf = textwrap.dedent("""\
            state: {a: {type: str}}
            agents:
              n: {prompt_text: hi, output: {ghost: json}}
            flow:
              entry: n
              edges: [{from: n, to: END}]
            """)
        d = make_skill(tmp_path, "badwf",
                       front="name: badwf\ndescription: 当需要时使用。\nmode: workflow\n",
                       extra={"workflow.yaml": wf})
        with pytest.raises(SkillError, match="state 段里没有它"):
            compile_workflow(load_skill(d))

    def test_edge_to_unknown_node_rejected(self, tmp_path):
        wf = textwrap.dedent("""\
            state: {a: {type: str}}
            agents:
              n: {prompt_text: hi, output: {a: text}}
            flow:
              entry: n
              edges: [{from: n, to: ghost}]
            """)
        d = make_skill(tmp_path, "badedge",
                       front="name: badedge\ndescription: 当需要时使用。\nmode: workflow\n",
                       extra={"workflow.yaml": wf})
        with pytest.raises(SkillError, match="编译失败"):
            compile_workflow(load_skill(d))


class TestRegistry:
    def test_l1_index_is_compact(self, registry):
        report = registry.l1_budget_report()
        assert report["tokens"] < report["budget"]
        assert all(s.name in registry.l1_index() for s in registry)

    def test_hot_reload(self, tmp_path):
        make_skill(tmp_path, "one")
        reg = SkillRegistry([tmp_path])
        assert reg.names() == ["one"]
        make_skill(tmp_path, "two")
        assert reg.reload().names() == ["one", "two"]


class TestCliErrors:
    """新手遇到的第一个错误不该是一页 traceback。"""

    def test_missing_llm_backend_gives_actionable_hint(self, capsys, tmp_path):
        from cloud_think.cli import main
        code = main(["--skills", str(SKILLS_DIR), "--db", str(tmp_path / "e.db"),
                     "--runs", str(tmp_path / "r"), "--llm", "ollama",
                     "run", "调研一下本地推理框架"])
        err = capsys.readouterr().err
        assert code == 1
        assert "LLM 后端" in err and "ollama serve" in err
        assert "run_demo.py" in err, "要给出零配置的替代路径"
        assert "Traceback" not in err

    def test_no_matching_skill_gives_actionable_hint(self, capsys, tmp_path):
        from cloud_think.cli import main
        code = main(["--skills", str(tmp_path / "empty"), "--db", str(tmp_path / "e.db"),
                     "--llm", "ollama", "run", "随便什么"])
        assert code == 1
        assert "--skill" in capsys.readouterr().err
