"""Skill 注册表：L1 索引、查找、热重载。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..core.errors import SkillError
from .loader import discover, load_skill
from .spec import Skill


class SkillRegistry:
    """所有已安装 skill 的索引。

    L1 = 每个 skill 的 name + description，常驻系统提示；预算控制在 2-3k token，
    大约支撑 30 个 skill。超了就上向量召回，L1 只放 top-k。
    """

    L1_TOKEN_BUDGET = 3000

    def __init__(self, roots: list[str | Path] | None = None) -> None:
        self.roots: list[Path] = [Path(r) for r in (roots or [])]
        self.skills: dict[str, Skill] = {}
        if self.roots:
            self.reload()

    def reload(self) -> SkillRegistry:
        """重扫磁盘。改完 SKILL.md 立即生效，不用重启。"""
        self.skills = {s.name: s for s in discover(self.roots)}
        return self

    def add(self, skill: Skill) -> SkillRegistry:
        self.skills[skill.name] = skill
        return self

    def add_path(self, path: str | Path) -> SkillRegistry:
        return self.add(load_skill(path))

    def get(self, name: str) -> Skill:
        if name not in self.skills:
            raise SkillError(f"未知 skill {name!r}，已安装: {sorted(self.skills)}")
        return self.skills[name]

    def __contains__(self, name: object) -> bool:
        return name in self.skills

    def __len__(self) -> int:
        return len(self.skills)

    def __iter__(self) -> Iterator[Skill]:
        return iter(self.skills.values())

    def names(self) -> list[str]:
        return sorted(self.skills)

    def l1_index(self, subset: list[str] | None = None) -> str:
        """给路由用的 L1 索引文本。"""
        items = [self.skills[n] for n in (subset or self.names()) if n in self.skills]
        return "\n".join(s.l1_entry() for s in items)

    def l1_budget_report(self) -> dict[str, int]:
        from ..llm.base import estimate_tokens
        text = self.l1_index()
        return {"skills": len(self.skills), "tokens": estimate_tokens(text),
                "budget": self.L1_TOKEN_BUDGET}
