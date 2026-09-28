"""命令行入口。

    ct skills                      列出已安装 skill 和 L1 预算
    ct lint                        校验所有 skill
    ct graph <skill>               导出编译后的 mermaid 图
    ct run "<任务>"                 路由 -> 编译 -> 执行
    ct resume <run_id> [--answer]  崩溃/暂停后恢复，或回答 interrupt
    ct fork <run_id> --from-step N 从第 N 步分叉重跑
    ct inspect <run_id> [--step N] 查看某步的状态
    ct trace <run_id>              时间线 + token 统计
    ct runs                        列出全部 run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .core.checkpoint import SQLiteCheckpointer
from .core.errors import CloudThinkError, NodeFailed, RoutingError, SkillError
from .core.events import EventBus
from .runtime import Runtime, RuntimeConfig
from .skills.compiler import compile_skill
from .skills.lint import lint_all
from .skills.registry import SkillRegistry
from .tools.builtin import LocalCorpusSearch, build_registry


def _make_llm(args: argparse.Namespace) -> Any:
    """按 --llm / 环境变量选择后端。本地小模型和云 API 走同一个协议。"""
    kind = (args.llm or os.environ.get("CT_LLM", "ollama")).lower()
    model = args.model or os.environ.get("CT_MODEL", "")
    if kind == "ollama":
        from .llm.http import OllamaClient
        return OllamaClient(model=model or "qwen2.5:7b",
                            host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    if kind == "anthropic":
        from .llm.http import AnthropicClient
        return AnthropicClient(model=model or "claude-sonnet-5")
    if kind == "openai":
        from .llm.http import OpenAICompatClient
        return OpenAICompatClient(model=model or "local",
                                  base_url=os.environ.get("OPENAI_BASE_URL",
                                                          "http://127.0.0.1:8000/v1"))
    raise SystemExit(f"未知 --llm {kind!r}，可选 ollama / anthropic / openai")


def _build_runtime(args: argparse.Namespace) -> Runtime:
    roots = [Path(r) for r in (args.skills or ["skills"])]
    corpus = Path(args.corpus) if getattr(args, "corpus", None) else None
    search = LocalCorpusSearch(path=corpus) if corpus and corpus.exists() else None
    tools = build_registry(search=search, workspace=Path.cwd())
    cfg = RuntimeConfig(skill_roots=roots, db_path=args.db, run_root=args.runs,
                        verbose=args.verbose)
    return Runtime(_make_llm(args), tools=tools, config=cfg)


def _kv(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--input 需要 key=value 形式，收到 {item!r}")
        k, v = item.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


# ------------------------------------------------------------------ 命令
def cmd_skills(args: argparse.Namespace) -> int:
    reg = SkillRegistry([Path(r) for r in (args.skills or ["skills"])])
    if not len(reg):
        print("没有找到任何 skill。目录:", args.skills or ["skills"])
        return 1
    for s in reg:
        wf = "workflow" if s.mode == "workflow" else "agentic"
        print(f"\n\033[1m{s.name}\033[0m  [{wf}] v{s.meta.version}")
        print(f"  {s.description}")
        if s.meta.tools:
            print(f"  工具: {', '.join(s.meta.tools)}")
        res = s.list_resources()
        if res:
            print(f"  资源: {len(res)} 个文件")
    b = reg.l1_budget_report()
    print(f"\nL1 索引: {b['skills']} 个 skill，约 {b['tokens']} token"
          f"（预算 {b['budget']}）")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    reg = SkillRegistry([Path(r) for r in (args.skills or ["skills"])])
    findings = lint_all(list(reg))
    for f in findings:
        color = "31" if f.level == "error" else "33"
        print(f"\033[{color}m{f}\033[0m")
    errors = sum(1 for f in findings if f.level == "error")
    warns = len(findings) - errors
    print(f"\n{len(reg)} 个 skill：{errors} 个错误，{warns} 个警告")
    return 1 if errors else 0


def cmd_graph(args: argparse.Namespace) -> int:
    reg = SkillRegistry([Path(r) for r in (args.skills or ["skills"])])
    graph = compile_skill(reg.get(args.skill))
    print(graph.to_mermaid())
    print("\n# 状态 channel")
    print(graph.schema.describe())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rt = _build_runtime(args)

    async def go() -> int:
        result = await rt.run_task(args.task, inputs=_kv(args.input),
                                   skill_name=args.skill)
        print(f"\n\033[1m状态\033[0m {result.status}  超步 {result.steps}  "
              f"run_id {result.run_id}")
        if result.status == "paused":
            print(f"\033[33m等待人工输入:\033[0m "
                  f"{json.dumps(result.interrupt, ensure_ascii=False, indent=2)}")
            print(f"回答后继续:  ct resume {result.run_id} --answer '\"yes\"'")
        else:
            print(json.dumps(result.state, ensure_ascii=False, indent=2, default=str)[:4000])
        print(f"产物目录: {result.run_dir}")
        return 0 if result.status in ("done", "paused") else 1

    return asyncio.run(go())


def cmd_resume(args: argparse.Namespace) -> int:
    rt = _build_runtime(args)

    async def go() -> int:
        answer = json.loads(args.answer) if args.answer else None
        result = await rt.resume(args.run_id, answer=answer)
        print(f"\n状态 {result.status}  超步 {result.steps}")
        if result.status == "paused":
            print(json.dumps(result.interrupt, ensure_ascii=False, indent=2))
        return 0 if result.status in ("done", "paused") else 1

    return asyncio.run(go())


def cmd_fork(args: argparse.Namespace) -> int:
    rt = _build_runtime(args)

    async def go() -> int:
        result = await rt.fork(args.run_id, args.from_step, _kv(args.set))
        print(f"\n分叉 run {result.run_id}  状态 {result.status}  超步 {result.steps}")
        return 0 if result.ok else 1

    return asyncio.run(go())


def cmd_inspect(args: argparse.Namespace) -> int:
    cp = SQLiteCheckpointer(args.db)

    async def go() -> int:
        steps = await cp.list_steps(args.run_id)
        if not steps:
            print(f"run {args.run_id!r} 没有 checkpoint")
            return 1
        step = args.step if args.step is not None else steps[-1]
        ckpt = await cp.load_at(args.run_id, step)
        if ckpt is None:
            print(f"没有 step {step}，可用: {steps}")
            return 1
        print(f"run {args.run_id}  step {step}/{steps[-1]}  状态 {ckpt.status}")
        print(f"frontier: {[t.instance for t in ckpt.frontier]}")
        print(f"visits: {ckpt.visits}")
        if ckpt.interrupt:
            print(f"interrupt: {json.dumps(ckpt.interrupt, ensure_ascii=False)}")
        print("\n状态:")
        print(json.dumps(ckpt.state, ensure_ascii=False, indent=2, default=str)[:6000])
        return 0

    return asyncio.run(go())


def cmd_trace(args: argparse.Namespace) -> int:
    cp = SQLiteCheckpointer(args.db)

    async def go() -> int:
        events = await cp.events(args.run_id)
        if not events:
            print(f"run {args.run_id!r} 没有事件")
            return 1
        t0 = events[0]["ts"]
        keep = {"run_started", "skill_selected", "skill_routed", "step_started",
                "node_finished", "node_failed", "node_retry", "state_updated",
                "interrupt", "run_paused", "run_finished", "run_failed",
                "script_run", "artifact_written"}
        for e in events:
            if not args.all and e["type"] not in keep:
                continue
            brief = {k: v for k, v in e["payload"].items()
                     if k not in ("state", "delta", "preview", "trace")}
            text = json.dumps(brief, ensure_ascii=False, default=str)
            if len(text) > 160:
                text = text[:160] + "…"
            print(f"{e['ts']-t0:7.2f}s  s{e['step']:<3} {e['type']:<20} "
                  f"{e['node'] or '':<16} {text}")
        runs = await cp.node_runs(args.run_id)
        usage = await cp.usage(args.run_id)
        print(f"\n节点执行 {len(runs)} 次；token in={usage['tokens_in']} "
              f"out={usage['tokens_out']}")
        return 0

    return asyncio.run(go())


def cmd_runs(args: argparse.Namespace) -> int:
    cp = SQLiteCheckpointer(args.db)

    async def go() -> int:
        for r in await cp.list_runs(args.limit):
            meta = json.loads(r.get("meta") or "{}")
            print(f"{r['run_id']:<22} {r['status']:<8} {meta.get('skill',''):<20} "
                  f"{(meta.get('task') or '')[:50]}")
        return 0

    return asyncio.run(go())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("ct", description="本地 skill 驱动的多 agent 工作流")
    p.add_argument("--skills", action="append", help="skill 目录（可多次）")
    p.add_argument("--db", default=os.environ.get("CT_DB", "state.db"))
    p.add_argument("--runs", default=os.environ.get("CT_RUNS", "runs"))
    p.add_argument("--llm", help="ollama | anthropic | openai")
    p.add_argument("--model")
    p.add_argument("--corpus", help="本地检索语料（JSON 文件或目录）")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("skills", help="列出 skill").set_defaults(fn=cmd_skills)
    sub.add_parser("lint", help="校验 skill").set_defaults(fn=cmd_lint)

    g = sub.add_parser("graph", help="导出 mermaid 图")
    g.add_argument("skill")
    g.set_defaults(fn=cmd_graph)

    r = sub.add_parser("run", help="执行一个任务")
    r.add_argument("task")
    r.add_argument("--skill", help="跳过路由，指定 skill")
    r.add_argument("--input", action="append", help="额外输入 key=value")
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("resume", help="恢复一个 run")
    rs.add_argument("run_id")
    rs.add_argument("--answer", help="回答 interrupt 的 JSON")
    rs.set_defaults(fn=cmd_resume)

    fk = sub.add_parser("fork", help="从某一步分叉重跑")
    fk.add_argument("run_id")
    fk.add_argument("--from-step", type=int, required=True, dest="from_step")
    fk.add_argument("--set", action="append", help="覆盖状态 key=value")
    fk.set_defaults(fn=cmd_fork)

    ins = sub.add_parser("inspect", help="查看某步状态")
    ins.add_argument("run_id")
    ins.add_argument("--step", type=int)
    ins.set_defaults(fn=cmd_inspect)

    tr = sub.add_parser("trace", help="查看时间线")
    tr.add_argument("run_id")
    tr.add_argument("--all", action="store_true")
    tr.set_defaults(fn=cmd_trace)

    ru = sub.add_parser("runs", help="列出 run")
    ru.add_argument("--limit", type=int, default=20)
    ru.set_defaults(fn=cmd_runs)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n已中断（checkpoint 已保存，可用 ct resume 继续）", file=sys.stderr)
        return 130
    except (CloudThinkError, KeyError) as e:
        _explain(e, args)
        return 1


def _explain(exc: BaseException, args: argparse.Namespace) -> None:
    """把异常翻译成可操作的提示。

    新手遇到的第一个错误几乎总是"没配 LLM 后端"，给他一页 traceback 帮不上忙。
    """
    detail = str(exc)
    print(f"\n\033[31m失败:\033[0m {detail}", file=sys.stderr)

    hint = ""
    if isinstance(exc, NodeFailed) and ("连接失败" in detail or "Connection refused" in detail):
        kind = (getattr(args, "llm", None) or os.environ.get("CT_LLM", "ollama"))
        hint = (f"看起来 LLM 后端（--llm {kind}）连不上。检查：\n"
                f"  • ollama:    ollama serve  然后  ollama pull qwen2.5:7b\n"
                f"  • anthropic: export CT_LLM=anthropic ANTHROPIC_API_KEY=...\n"
                f"  • 兼容端点:   export CT_LLM=openai OPENAI_BASE_URL=http://...\n"
                f"  • 只想先看看流程：python examples/run_demo.py（不需要任何后端）")
    elif isinstance(exc, NodeFailed):
        hint = ("节点重试耗尽。checkpoint 已保存，修好之后可以接着跑：\n"
                f"  ct --db {getattr(args, 'db', 'state.db')} runs        # 找到 run_id\n"
                f"  ct --db {getattr(args, 'db', 'state.db')} trace <run_id>   # 看失败在哪\n"
                f"  ct --db {getattr(args, 'db', 'state.db')} resume <run_id>  # 从断点继续")
    elif isinstance(exc, RoutingError):
        hint = ("没有 skill 匹配这个任务。可以：\n"
                "  • 用 --skill <name> 跳过路由直接指定\n"
                "  • ct lint 检查 description 有没有写清楚触发条件\n"
                "  • ct skills 看一眼装了哪些 skill")
    elif isinstance(exc, SkillError):
        hint = "skill 包有问题，跑 ct lint 看完整校验结果。"
    if hint:
        print(f"\n{hint}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
