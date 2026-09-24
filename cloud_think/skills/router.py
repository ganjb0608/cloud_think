"""Skill 路由：复杂任务进来，决定用哪个（哪些）skill。

三条路径：显式调用 > LLM 语义路由 > BM25 关键词兜底。
路由决策全部记事件——skill 选不中时 90% 是 description 写得像介绍而不像触发条件，
有日志才定位得到。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import RoutingError
from ..llm.base import Message, extract_json
from .registry import SkillRegistry
from .spec import Skill

_WORD = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]")
_EXPLICIT = re.compile(r"(?:^|\s)/([a-z0-9][a-z0-9._-]*)")

ROUTER_SYSTEM = """你是一个 skill 路由器。下面是本地已安装的能力包清单，每行是「名称: 什么时候用它」。

{index}

用户任务：
{task}

选出最合适的 skill（可以多选，但只在任务确实需要组合能力时才多选；没有合适的就返回空列表）。
只输出 JSON，不要任何解释：
{{"skills": ["名称", ...], "confidence": 0.0-1.0, "reason": "一句话理由"}}"""


@dataclass
class SkillMatch:
    name: str
    score: float
    via: str                       # explicit | llm | keyword
    reason: str = ""


@dataclass
class RouteResult:
    matches: list[SkillMatch] = field(default_factory=list)
    candidates: list[SkillMatch] = field(default_factory=list)
    via: str = "none"
    task: str = ""

    @property
    def names(self) -> list[str]:
        return [m.name for m in self.matches]

    @property
    def top(self) -> SkillMatch | None:
        return self.matches[0] if self.matches else None


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


class SkillRouter:
    def __init__(self, registry: SkillRegistry, llm: Any = None,
                 bus: Any = None, min_confidence: float = 0.35,
                 max_skills: int = 2) -> None:
        self.registry = registry
        self.llm = llm
        self.bus = bus
        self.min_confidence = min_confidence
        self.max_skills = max_skills

    # ---------------------------------------------------------------- 主入口
    async def route(self, task: str) -> RouteResult:
        explicit = self._explicit(task)
        if explicit:
            result = RouteResult(matches=explicit, candidates=explicit, via="explicit", task=task)
            self._log(result)
            return result

        keyword = self._keyword_rank(task)

        if self.llm is not None:
            try:
                matches = await self._llm_route(task, keyword)
                result = RouteResult(matches=matches, candidates=keyword, via="llm", task=task)
                self._log(result)
                return result
            except Exception as e:  # 路由失败不该让整个任务挂掉，降级到关键词
                self._emit("skill_route_degraded", error=f"{type(e).__name__}: {e}")

        picked = [m for m in keyword[:1] if m.score >= self.min_confidence]
        result = RouteResult(matches=picked, candidates=keyword, via="keyword", task=task)
        self._log(result)
        return result

    def resolve(self, result: RouteResult) -> list[Skill]:
        """把匹配结果变成 skill 对象，并处理 conflicts_with。"""
        skills: list[Skill] = []
        for m in result.matches:
            s = self.registry.get(m.name)
            conflict = next((p for p in skills if p.name in s.meta.conflicts_with
                             or s.name in p.meta.conflicts_with), None)
            if conflict:
                self._emit("skill_conflict", dropped=s.name, kept=conflict.name)
                continue
            skills.append(s)
        return skills

    # ---------------------------------------------------------------- 三条路径
    def _explicit(self, task: str) -> list[SkillMatch]:
        out = []
        for name in _EXPLICIT.findall(task):
            if name in self.registry:
                out.append(SkillMatch(name, 1.0, "explicit", "用户显式调用"))
        return out

    def _keyword_rank(self, task: str) -> list[SkillMatch]:
        """BM25 兜底召回。

        这里是接入本地 embedding 的位置：skill 数量超过 L1 预算时，
        把这个方法换成向量检索，返回结构不变。
        """
        docs = list(self.registry)
        if not docs:
            return []
        corpora = [_tokenize(f"{s.meta.name} {s.meta.description} "
                             f"{' '.join(s.meta.keywords)} {s.body[:2000]}") for s in docs]
        df: Counter[str] = Counter()
        for toks in corpora:
            df.update(set(toks))
        n = len(docs)
        avg = sum(len(c) for c in corpora) / n
        q = _tokenize(task)
        k1, b = 1.5, 0.75
        scored: list[SkillMatch] = []
        for skill, toks in zip(docs, corpora):
            tf = Counter(toks)
            score = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(toks) / avg))
            scored.append(SkillMatch(skill.name, score, "keyword"))
        top = max((m.score for m in scored), default=0.0)
        for m in scored:      # 归一化到 0-1，便于和 min_confidence 比较
            m.score = round(m.score / top, 3) if top > 0 else 0.0
        scored.sort(key=lambda m: m.score, reverse=True)
        return [m for m in scored if m.score > 0]

    async def _llm_route(self, task: str, keyword: list[SkillMatch]) -> list[SkillMatch]:
        index = self.registry.l1_index()
        if not index.strip():
            return []
        prompt = ROUTER_SYSTEM.format(index=index, task=task)
        resp = await self.llm.complete([Message("user", prompt)])
        data = extract_json(resp.text)
        if not isinstance(data, dict):
            raise RoutingError(f"路由器返回了非对象: {data!r}")
        confidence = float(data.get("confidence", 0.0))
        reason = str(data.get("reason", ""))
        names = [n for n in (data.get("skills") or []) if n in self.registry]
        if confidence < self.min_confidence:
            self._emit("skill_route_low_confidence", confidence=confidence, reason=reason)
            return []
        return [SkillMatch(n, confidence, "llm", reason) for n in names[:self.max_skills]]

    # ---------------------------------------------------------------- 日志
    def _log(self, result: RouteResult) -> None:
        self._emit("skill_routed", via=result.via, chosen=result.names,
                   candidates=[(m.name, m.score) for m in result.candidates[:5]],
                   reason=result.top.reason if result.top else "")

    def _emit(self, type: str, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.emit(type, **payload)
