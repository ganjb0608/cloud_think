#!/usr/bin/env python3
"""端到端演示：用 research-report skill 处理一个真实的复杂任务。

用脚本化 LLM 驱动，所以不需要 ollama、不需要 API key 就能跑：

    python examples/run_demo.py

跑完后可以用 CLI 检查这次运行的全过程：

    ct --db demo.db --runs demo_runs trace <run_id>
    ct --db demo.db --runs demo_runs inspect <run_id> --step 3
    ct --db demo.db --runs demo_runs fork <run_id> --from-step 3
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from cloud_think.core.checkpoint import SQLiteCheckpointer          # noqa: E402
from cloud_think.core.events import ConsoleSink, EventBus, JsonlSink  # noqa: E402
from cloud_think.runtime import Runtime, RuntimeConfig              # noqa: E402
from cloud_think.skills.registry import SkillRegistry               # noqa: E402
from cloud_think.tools.builtin import LocalCorpusSearch, build_registry  # noqa: E402
from examples.scripted_research import build_research_llm           # noqa: E402

TASK = "调研一下 2026 年本地 LLM 推理框架的现状，出一份带引用的报告"

DB = ROOT / "demo.db"
RUNS = ROOT / "demo_runs"


def banner(text: str) -> None:
    print(f"\n\033[1;36m{'─' * 70}\n{text}\n{'─' * 70}\033[0m")


async def main() -> int:
    for p in (DB, RUNS):
        if p.exists():
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    llm = build_research_llm()
    search = LocalCorpusSearch(path=ROOT / "tests" / "fixtures" / "corpus.json")
    tools = build_registry(search=search, workspace=ROOT)
    registry = SkillRegistry([ROOT / "skills"])
    cp = SQLiteCheckpointer(DB)
    bus = EventBus([ConsoleSink(stream=sys.stdout), cp])

    rt = Runtime(llm=llm, tools=tools, registry=registry, checkpointer=cp, bus=bus,
                 config=RuntimeConfig(run_root=RUNS, console=False, max_steps=30))

    banner(f"已安装 skill（L1 索引 {registry.l1_budget_report()['tokens']} token）")
    for s in registry:
        print(f"  {s.name:<18} [{s.mode}]  {s.description[:60]}…")

    banner("第 1 步：路由 —— 这个任务该用哪个 skill")
    route, skills = await rt.route(TASK)
    print(f"  任务: {TASK}")
    print(f"  方式: {route.via}   选中: {[s.name for s in skills]}")
    print(f"  候选: {[(m.name, m.score) for m in route.candidates]}")

    banner("第 2 步：编译 workflow.yaml 并执行（会停在发布前的人工批准点）")
    result = await rt.run_task(TASK)

    banner("第 3 步：暂停等待人工批准")
    print(f"  状态: {result.status}")
    print(f"  挂起节点: {result.interrupt['node']}")
    print(f"  已完成 {result.steps} 个超步，修订 {result.state['revision']} 次，"
          f"审稿分 {result.state['review']['score']}")
    print(f"  脚本检出数值分歧 {len(result.state['conflicts'])} 处:")
    for c in result.state["conflicts"]:
        print(f"    {c['a']['value']} ({c['a']['source']})  vs  "
              f"{c['b']['value']} ({c['b']['source']})")

    banner("第 4 步：批准后恢复，完成发布")
    final = await rt.resume(result.run_id, answer={"approved": True})
    print(f"  状态: {final.status}")

    banner("产出")
    report = final.run_dir / final.state["report_ref"]
    print(f"  终稿: {report}")
    for f in sorted((final.run_dir / "artifacts").iterdir()):
        print(f"    {f.name:<22} {f.stat().st_size:>6} 字节")

    usage = await cp.usage(final.run_id)
    runs = await cp.node_runs(final.run_id)
    banner("统计")
    print(f"  run_id      {final.run_id}")
    print(f"  超步         {final.steps}")
    print(f"  节点执行     {len(runs)} 次")
    print(f"  token        in={usage['tokens_in']}  out={usage['tokens_out']}")
    print(f"  LLM 调用     {len(llm.calls)} 次")

    banner("终稿前 40 行")
    print("\n".join(report.read_text(encoding="utf-8").splitlines()[:40]))

    print(f"\n继续探查:\n  ct --db {DB.name} --runs {RUNS.name} trace {final.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
