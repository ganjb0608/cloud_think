#!/usr/bin/env python3
"""新手教程配套示例：changelog-digest。

    python examples/run_tutorial.py

用脚本化 LLM 跑通「4 条提交 -> 并行分类 -> 脚本归组 -> 写发布说明」，
不需要 ollama 也不需要 API key。docs/TUTORIAL.md 里的每一步都对应这里的代码。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from cloud_think.core.checkpoint import MemoryCheckpointer          # noqa: E402
from cloud_think.core.events import EventBus, MemorySink            # noqa: E402
from cloud_think.llm.mock import ScriptedLLM                        # noqa: E402
from cloud_think.runtime import Runtime, RuntimeConfig              # noqa: E402
from cloud_think.skills.registry import SkillRegistry               # noqa: E402
from cloud_think.tools.builtin import build_registry                # noqa: E402

TUTORIAL_SKILLS = ROOT / "examples" / "tutorial_skills"

COMMITS = [
    "feat: 新增 ct fork 命令，可从任意超步分叉重跑",
    "fix: 修复 EventBus 重复挂载同一 sink 导致事件写两遍",
    "refactor!: ctx.effect 签名改为 (key, fn)，旧调用需要改",
    "chore: 补充 CI 缓存配置",
]

#: 每条提交的预期分类，用来驱动脚本化 LLM
KINDS = {
    COMMITS[0]: ("feature", True),
    COMMITS[1]: ("fix", True),
    COMMITS[2]: ("breaking", True),
    COMMITS[3]: ("internal", False),
}

NOTES = """## 不兼容变更
- ctx.effect 签名改为 (key, fn)，旧调用需要改

## 新功能
- 新增 ct fork 命令，可从任意超步分叉重跑

## 修复
- 修复 EventBus 重复挂载同一 sink 导致事件写两遍
"""


def build_llm() -> ScriptedLLM:
    llm = ScriptedLLM()

    def classifier(messages):
        text = "\n".join(m.content for m in messages)
        mine = re.search(r"# 你这一份的输入\n(.+)", text).group(1).strip()
        kind, user_facing = KINDS[mine]
        return {"kind": kind, "summary": mine.split(": ", 1)[-1],
                "user_facing": user_facing}

    blob = lambda ms: "\n".join(m.content for m in ms)  # noqa: E731
    llm.add(lambda ms: "ROLE: classifier" in blob(ms), classifier)
    llm.add(lambda ms: "ROLE: writer" in blob(ms), NOTES)
    return llm


def build_runtime(run_root, llm=None, sink=None, cp=None) -> Runtime:
    return Runtime(
        llm=llm or build_llm(),
        tools=build_registry(workspace=ROOT),
        registry=SkillRegistry([TUTORIAL_SKILLS]),
        checkpointer=cp or MemoryCheckpointer(),
        bus=EventBus([sink] if sink else []),
        config=RuntimeConfig(run_root=run_root, console=False),
    )


async def main() -> int:
    sink = MemorySink()
    rt = build_runtime("/tmp/ct_tutorial_runs", sink=sink)

    result = await rt.run_skill("changelog-digest", task="v0.2.0",
                                inputs={"commits": COMMITS})

    print(f"状态 {result.status}   超步 {result.steps}")
    fanout = sorted(e.node for e in sink.of("node_finished")
                    if e.node.startswith("classifier"))
    print(f"并行分类实例 {fanout}")
    print(f"脚本归组统计 {[e.payload['_stats'] for e in sink.of('script_stats')]}")
    print(f"进入正文的类别 {list(result.state['buckets'])}（internal 已被脚本剔除）")
    print(f"\n产物 {result.run_dir / result.state['notes_ref']}\n")
    print((result.run_dir / result.state["notes_ref"]).read_text(encoding="utf-8"))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
