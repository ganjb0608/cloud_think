"""节点级重试策略：指数退避 + 抖动，区分可重试与不可重试异常。"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetryPolicy:
    """重试配置。

    ``retry_on`` 是可重试异常（网络抖动、限流、超时）；``give_up_on`` 优先级更高，
    命中即放弃——参数校验错误、内容策略拒绝重试多少次都是一样的结果，只是烧配额。
    """

    max_attempts: int = 1
    base: float = 1.0
    factor: float = 2.0
    max_delay: float = 30.0
    jitter: bool = True
    retry_on: tuple[type[BaseException], ...] = (Exception,)
    give_up_on: tuple[type[BaseException], ...] = field(default_factory=tuple)

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        if self.give_up_on and isinstance(exc, self.give_up_on):
            return False
        return isinstance(exc, self.retry_on)

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次尝试失败后的等待秒数（attempt 从 1 起）。"""
        d = min(self.base * (self.factor ** (attempt - 1)), self.max_delay)
        if self.jitter:
            d *= 0.5 + random.random() * 0.5  # noqa: S311 - 退避抖动，非密码学用途
        return d


NO_RETRY = RetryPolicy(max_attempts=1)
