"""节点内部的 LLM ↔ 工具微循环。

注意这是**节点内**的循环，不占超步。超步循环留给 agent 之间的协作——
两者混在一起会让 checkpoint 粒度失控。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ..core.context import Ctx
from ..llm.base import LLMResponse, Message, Usage
from ..tools.registry import ToolRegistry


async def _call_tool(ctx: Ctx, tools: ToolRegistry, call: Any) -> Message:
    try:
        tool = tools.get(call.name)
        result = await tool.call(ctx, **(call.args or {}))
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        ctx.emit("tool_call", tool=call.name, args=call.args, ok=True, chars=len(text))
    except Exception as e:
        # 工具报错回灌给模型，让它自己纠正，而不是直接炸掉节点
        text = f"工具执行失败: {type(e).__name__}: {e}"
        ctx.emit("tool_call", tool=call.name, args=call.args, ok=False, error=str(e))
    return Message(role="tool", content=text, tool_call_id=call.id, name=call.name)


async def tool_loop(
    ctx: Ctx, messages: list[Message], tools: ToolRegistry,
    max_rounds: int = 8, **llm_kwargs: Any,
) -> tuple[LLMResponse, list[Message]]:
    """跑完整的工具调用循环，返回最后一次响应和完整消息历史。"""
    llm = ctx.llm
    if llm is None:
        raise RuntimeError("上下文里没有 LLM client")
    schemas = tools.schemas() if len(tools) else None
    history = list(messages)
    total = Usage()
    last: LLMResponse | None = None

    for round_no in range(max_rounds):
        resp = await llm.complete(history, tools=schemas, **llm_kwargs)
        total = total + resp.usage
        last = resp
        ctx.emit("llm_call", round=round_no, tokens_in=resp.usage.tokens_in,
                 tokens_out=resp.usage.tokens_out, tool_calls=[c.name for c in resp.tool_calls])
        if not resp.tool_calls:
            break
        history.append(Message(role="assistant", content=resp.text or ""))
        results = await asyncio.gather(*[_call_tool(ctx, tools, c) for c in resp.tool_calls])
        history.extend(results)
    else:
        ctx.emit("tool_loop_exhausted", max_rounds=max_rounds)

    assert last is not None
    last.usage = total
    ctx.add_usage(total.tokens_in, total.tokens_out)
    return last, history
