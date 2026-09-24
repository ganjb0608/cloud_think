"""状态模型：Channel + Reducer。

核心约束：节点不直接修改 state，只返回增量（delta）。引擎把同一超步内所有
delta 按 channel 的 reducer 合并。这是并行安全、可检查点、可重放的前提。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .errors import ConflictError

Delta = Mapping[str, Any]
Reducer = Callable[[Any, Any], Any]


def r_replace(old: Any, new: Any) -> Any:
    """整体替换。独占语义：同超步被多写会在引擎层报 ConflictError。"""
    return new


def r_last(old: Any, new: Any) -> Any:
    """显式的最后写入生效，允许并发写（作者自己确认过无所谓顺序）。"""
    return new


def r_add(old: Any, new: Any) -> Any:
    """数值相加 / 序列拼接。"""
    if old is None:
        return new
    if new is None:
        return old
    return old + new


def r_merge(old: Any, new: Any) -> Any:
    """字典浅合并，新值覆盖同名 key。多 agent 各写各的 key 时用这个。"""
    if not isinstance(old, Mapping):
        old = {}
    if not isinstance(new, Mapping):
        raise TypeError(f"merge reducer 需要 dict 增量，收到 {type(new).__name__}")
    return {**old, **new}


def r_append(old: Any, new: Any) -> Any:
    """把单个元素追加到列表。"""
    return list(old or []) + [new]


def r_extend(old: Any, new: Any) -> Any:
    """把一个列表展开追加。"""
    return list(old or []) + list(new or [])


def r_union(old: Any, new: Any) -> Any:
    """列表去重并集，保持顺序。"""
    out = list(old or [])
    seen = {repr(x) for x in out}
    for x in new or []:
        if repr(x) not in seen:
            out.append(x)
            seen.add(repr(x))
    return out


def r_max(old: Any, new: Any) -> Any:
    return new if old is None else max(old, new)


REDUCERS: dict[str, Reducer] = {
    "replace": r_replace,
    "last": r_last,
    "add": r_add,
    "merge": r_merge,
    "append": r_append,
    "extend": r_extend,
    "union": r_union,
    "max": r_max,
}

#: 这些 reducer 在同一超步内被多个节点写入时直接报错，而不是让结果取决于调度顺序。
EXCLUSIVE_REDUCERS = frozenset({"replace"})

_DEFAULTS: dict[str, Any] = {
    "str": "", "int": 0, "float": 0.0, "bool": False,
    "list": list, "dict": dict, "any": None,
}


@dataclass(frozen=True)
class Channel:
    """状态里的一个字段，带合并语义。"""

    name: str
    type: str = "any"
    reducer: str = "replace"
    default: Any = None
    has_default: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.reducer not in REDUCERS:
            raise ValueError(
                f"channel {self.name!r} 的 reducer {self.reducer!r} 未知，"
                f"可选: {', '.join(sorted(REDUCERS))}"
            )

    @property
    def exclusive(self) -> bool:
        return self.reducer in EXCLUSIVE_REDUCERS

    def initial(self) -> Any:
        if self.has_default:
            return self.default
        d = _DEFAULTS.get(self.type, None)
        return d() if callable(d) else d

    def fold(self, old: Any, new: Any) -> Any:
        return REDUCERS[self.reducer](old, new)


class StateSchema:
    """一组 channel 的集合，负责初始化与 delta 合并。"""

    def __init__(self, channels: Iterable[Channel]) -> None:
        self.channels: dict[str, Channel] = {c.name: c for c in channels}

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> StateSchema:
        """从 workflow.yaml 的 ``state:`` 段构建。

        支持 ``{"topic": {"type": "str"}}`` 和简写 ``{"topic": "str"}``。
        """
        channels = []
        for name, cfg in (spec or {}).items():
            if isinstance(cfg, str):
                cfg = {"type": cfg}
            cfg = dict(cfg or {})
            channels.append(Channel(
                name=name,
                type=cfg.get("type", "any"),
                reducer=cfg.get("reducer", "replace"),
                default=cfg.get("default"),
                has_default="default" in cfg,
                description=cfg.get("description", ""),
            ))
        return cls(channels)

    @property
    def names(self) -> set[str]:
        return set(self.channels)

    def initial(self, inputs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        state = {name: ch.initial() for name, ch in self.channels.items()}
        for k, v in (inputs or {}).items():
            if k not in self.channels:
                raise KeyError(f"输入 {k!r} 不是已声明的 channel，已声明: {sorted(self.channels)}")
            state[k] = v
        return state

    def apply(
        self, state: Mapping[str, Any], writes: list[tuple[str, Delta]]
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """把一个超步内所有节点的 delta 合并进 state。

        ``writes`` 是 ``(writer_id, delta)`` 列表。返回 (新 state, 每个被改动 channel
        的写入者列表)。独占 channel 被多写时抛 ConflictError。
        """
        by_channel: dict[str, list[tuple[str, Any]]] = {}
        for writer, delta in writes:
            if not delta:
                continue
            for key, value in delta.items():
                if key not in self.channels:
                    raise KeyError(
                        f"节点 {writer!r} 写入了未声明的 channel {key!r}，"
                        f"已声明: {sorted(self.channels)}"
                    )
                by_channel.setdefault(key, []).append((writer, value))

        new_state = dict(state)
        touched: dict[str, list[str]] = {}
        for key, pairs in by_channel.items():
            ch = self.channels[key]
            if len(pairs) > 1 and ch.exclusive:
                raise ConflictError(key, [w for w, _ in pairs])
            acc = new_state.get(key, ch.initial())
            for _, value in pairs:
                acc = ch.fold(acc, value)
            new_state[key] = acc
            touched[key] = [w for w, _ in pairs]
        return new_state, touched

    def describe(self) -> str:
        lines = []
        for name, ch in self.channels.items():
            desc = f"  - {name} ({ch.type}, reducer={ch.reducer})"
            if ch.description:
                desc += f": {ch.description}"
            lines.append(desc)
        return "\n".join(lines)
