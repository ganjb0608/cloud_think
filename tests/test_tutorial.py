"""守住 docs/TUTORIAL.md 里的示例：教程写的每一步都必须真能跑。"""
from __future__ import annotations

from cloud_think.core.events import MemorySink
from cloud_think.skills.compiler import compile_workflow
from cloud_think.skills.lint import lint_skill
from cloud_think.skills.registry import SkillRegistry
from examples.run_tutorial import COMMITS, TUTORIAL_SKILLS, build_runtime


def test_tutorial_skill_lints_clean():
    skill = SkillRegistry([TUTORIAL_SKILLS]).get("changelog-digest")
    assert [str(f) for f in lint_skill(skill) if f.level == "error"] == []


def test_tutorial_workflow_compiles():
    skill = SkillRegistry([TUTORIAL_SKILLS]).get("changelog-digest")
    g = compile_workflow(skill)
    assert set(g.nodes) == {"classifier", "group", "writer"}
    assert g.nodes["classifier"].parallel_over == "commits"
    assert g.nodes["group"].join == "all"


async def test_tutorial_example_runs_end_to_end(tmp_path):
    sink = MemorySink()
    rt = build_runtime(tmp_path / "runs", sink=sink)
    r = await rt.run_skill("changelog-digest", task="v0.2.0",
                           inputs={"commits": COMMITS})

    assert r.status == "done" and r.steps == 3
    # 4 条提交 -> 4 个并行实例，各自独立上下文
    assert sorted(e.node for e in sink.of("node_finished")
                  if e.node.startswith("classifier")) == \
        ["classifier#0", "classifier#1", "classifier#2", "classifier#3"]
    # 脚本剔掉了 internal，writer 根本看不到它
    assert list(r.state["buckets"]) == ["breaking", "feature", "fix"]
    assert sink.of("script_stats")[0].payload["_stats"]["internal"] == 1

    notes = (r.run_dir / r.state["notes_ref"]).read_text(encoding="utf-8")
    assert notes.startswith("## 不兼容变更"), "breaking 必须在最前面"
    assert "CI 缓存" not in notes, "internal 提交不该出现在发布说明里"


def test_integration_templates_are_valid_skills():
    """docs/integrations/ 下的包装 skill 模板必须本身就是合法 skill，
    否则用户复制过去会踩坑。"""
    from pathlib import Path
    root = Path(__file__).parent.parent / "docs" / "integrations" / "claude-code"
    reg = SkillRegistry([root])
    assert reg.names() == ["deep-task"]
    skill = reg.get("deep-task")
    assert [str(f) for f in lint_skill(skill) if f.level == "error"] == []
    # 模板的核心告诫：不要把中间产物全文读回外层上下文
    assert "不要把" in skill.body and "artifacts" in skill.body


def test_tutorial_references_existing_files():
    """教程附录里指向的文件都得真的存在。"""
    import re
    from pathlib import Path
    root = Path(__file__).parent.parent
    doc = (root / "docs" / "TUTORIAL.md").read_text(encoding="utf-8")
    paths = set(re.findall(r"`((?:cloud_think|skills|examples|tests|docs)/[\w./-]+)`", doc))
    missing = sorted(p for p in paths if not (root / p).exists())
    assert not missing, f"教程引用了不存在的路径: {missing}"
