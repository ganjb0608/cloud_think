"""事件总线：引擎、agent、LLM 层统一往这里打点，sink 可插拔。"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Event:
    type: str
    run_id: str = ""
    step: int = -1
    node: str = ""
    ts: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type, "run_id": self.run_id, "step": self.step,
            "node": self.node, "ts": self.ts, "payload": self.payload,
        }


class EventSink(Protocol):
    def handle(self, event: Event) -> None: ...


class ConsoleSink:
    """终端实时输出。不引入 rich 依赖，直接用 ANSI。"""

    COLORS = {
        "run_started": "1;36", "run_finished": "1;32", "run_failed": "1;31",
        "step_started": "36", "node_started": "34", "node_finished": "32",
        "node_failed": "31", "node_retry": "33", "state_updated": "35",
        "llm_call": "90", "tool_call": "90", "script_run": "90",
        "interrupt": "1;33", "skill_routed": "1;36", "subagent": "90",
    }

    def __init__(self, stream: Any = None, verbose: bool = False) -> None:
        self.stream = stream or sys.stderr
        self.verbose = verbose

    def handle(self, event: Event) -> None:
        quiet = {"llm_call", "tool_call", "subagent", "state_updated"}
        if not self.verbose and event.type in quiet:
            return
        color = self.COLORS.get(event.type, "0")
        loc = f"step {event.step}" if event.step >= 0 else ""
        if event.node:
            loc = f"{loc} {event.node}".strip()
        # trace/preview 这类大字段只留在事件表里（ct trace --all 能查），
        # 终端上刷一页堆栈对人没有帮助。
        bulky = ("state", "delta", "messages", "trace", "preview")
        brief = {k: v for k, v in event.payload.items() if k not in bulky}
        text = json.dumps(brief, ensure_ascii=False, default=str) if brief else ""
        if len(text) > 300:
            text = text[:300] + "…"
        print(f"\033[{color}m[{event.type}]\033[0m {loc} {text}", file=self.stream, flush=True)


class JsonlSink:
    """落盘成 jsonl，供事后 trace 分析。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.sink_key = f"jsonl:{self.path.resolve()}"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def handle(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")


class MemorySink:
    """测试用：把事件留在内存里做断言。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list[Event]:
        return [e for e in self.events if e.type == type_]


class EventBus:
    def __init__(self, sinks: list[EventSink] | None = None) -> None:
        self.sinks: list[EventSink] = []
        for sink in (sinks or []):
            self.add(sink)

    @staticmethod
    def _key(sink: EventSink) -> Any:
        """同一个目的地只挂一次。

        装配层和调用方各自 add 一遍同一个 sink 是很自然的写法，
        不去重的话每条事件会被写入两次，trace 直接读不了。
        """
        return getattr(sink, "sink_key", None) or id(sink)

    def add(self, sink: EventSink) -> EventBus:
        key = self._key(sink)
        if any(self._key(s) == key for s in self.sinks):
            return self
        self.sinks.append(sink)
        return self

    def emit(self, type: str, run_id: str = "", step: int = -1, node: str = "", **payload: Any) -> None:
        event = Event(type=type, run_id=run_id, step=step, node=node, payload=payload)
        for sink in self.sinks:
            try:
                sink.handle(event)
            except Exception as e:  # sink 故障绝不能打断工作流
                print(f"[event-sink-error] {type_name(sink)}: {e}", file=sys.stderr)


def type_name(obj: Any) -> str:
    return type(obj).__name__
