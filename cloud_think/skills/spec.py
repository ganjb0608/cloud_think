"""Skill 数据模型。

一个 skill 就是一个目录：SKILL.md（元数据 + 流程）+ 可选的 workflow.yaml、
prompts/、references/、scripts/、templates/。加一类复杂任务 = 加一个目录，不改代码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import SkillError

MODE_AGENTIC = "agentic"        # SKILL.md 就是流程说明，由 orchestrator 自行编排
MODE_WORKFLOW = "workflow"      # workflow.yaml 编译成图，交给超步引擎确定性执行


@dataclass
class SkillMeta:
    """frontmatter 里的元数据。``description`` 是路由的唯一依据。"""

    name: str
    description: str
    version: str = "0.1"
    mode: str = MODE_AGENTIC
    tools: list[str] = field(default_factory=list)
    max_steps: int = 30
    conflicts_with: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    author: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    meta: SkillMeta
    body: str                     # SKILL.md 正文（L2）
    path: Path
    workflow: dict[str, Any] | None = None    # 解析后的 workflow.yaml

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def description(self) -> str:
        return self.meta.description

    @property
    def mode(self) -> str:
        return self.meta.mode

    # ---- L1：只占几十 token，常驻系统提示 ----
    def l1_entry(self) -> str:
        return f"- {self.meta.name}: {self.meta.description}"

    # ---- L3：按需加载的资源 ----
    def resource_path(self, name: str) -> Path:
        """解析 skill 内的相对路径，并阻止 ../ 越界。"""
        p = (self.path / name).resolve()
        if not str(p).startswith(str(self.path.resolve())):
            raise SkillError(f"资源路径 {name!r} 越出 skill 目录 {self.path}")
        if not p.exists():
            raise SkillError(f"skill {self.name!r} 中不存在资源 {name!r}")
        return p

    def read_resource(self, name: str) -> str:
        return self.resource_path(name).read_text(encoding="utf-8")

    def list_resources(self, subdir: str = "") -> list[str]:
        base = self.path / subdir if subdir else self.path
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(self.path)) for p in base.rglob("*") if p.is_file())

    def prompt(self, name: str) -> str:
        return self.read_resource(name)

    def __repr__(self) -> str:
        return f"<Skill {self.meta.name} mode={self.meta.mode} at {self.path}>"
