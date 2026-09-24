"""引擎异常体系。Interrupt 是控制流信号，不是错误。"""
from __future__ import annotations

from typing import Any


class CloudThinkError(Exception):
    """所有本项目异常的基类。"""


class GraphError(CloudThinkError):
    """图构建或编译期错误（孤儿节点、悬空边、缺 entry）。"""


class ConflictError(CloudThinkError):
    """同一超步内多个节点写入了独占 channel。

    这是设计上的强制检查：默认 reducer 为 ``replace`` 的 channel 一旦被并发写入，
    结果取决于调度顺序，属于隐蔽 bug。此处直接失败，逼作者显式声明 reducer。
    """

    def __init__(self, channel: str, writers: list[str]) -> None:
        self.channel = channel
        self.writers = writers
        super().__init__(
            f"channel {channel!r} 在同一超步内被多个节点写入: {writers}。"
            f"请为该 channel 显式声明 reducer（add/merge/append/extend/last）。"
        )


class NodeFailed(CloudThinkError):
    """节点重试耗尽后仍然失败。"""

    def __init__(self, node: str, attempts: int, cause: BaseException) -> None:
        self.node = node
        self.attempts = attempts
        self.cause = cause
        super().__init__(f"节点 {node!r} 在 {attempts} 次尝试后失败: {cause!r}")


class MaxStepsExceeded(CloudThinkError):
    """超过全局超步上限，通常意味着图里有停不下来的环。"""


class MaxVisitsExceeded(CloudThinkError):
    """单节点访问次数超限，通常是 reflection loop 打转。"""


class SkillError(CloudThinkError):
    """skill 包加载/校验失败。"""


class RoutingError(CloudThinkError):
    """skill 路由失败。"""


class SandboxError(CloudThinkError):
    """脚本沙箱执行失败（超时、非零退出、输出超限）。"""


class ExpressionError(CloudThinkError):
    """条件表达式不合法或求值失败。"""


class OutputParseError(CloudThinkError):
    """LLM 输出不符合声明的 output schema。"""


class Interrupt(BaseException):  # noqa: N818 - 控制流信号，故意不继承 Exception
    """人工介入信号。

    继承 ``BaseException`` 而非 ``Exception``，这样节点内部宽泛的
    ``except Exception`` 不会把它吞掉。
    """

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        super().__init__(f"interrupt: {payload!r}")
