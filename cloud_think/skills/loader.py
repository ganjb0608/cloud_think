"""从磁盘加载 skill 包：解析 frontmatter、校验、读 workflow.yaml。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import SkillError
from .spec import MODE_AGENTIC, MODE_WORKFLOW, Skill, SkillMeta

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

_KNOWN_KEYS = {
    "name", "description", "version", "mode", "tools", "max_steps",
    "conflicts_with", "keywords", "author",
}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER.match(text)
    if not m:
        raise SkillError("SKILL.md 缺少 YAML frontmatter（文件必须以 --- 开头）")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"frontmatter YAML 解析失败: {e}") from e
    if not isinstance(meta, dict):
        raise SkillError("frontmatter 必须是一个 YAML 映射")
    return meta, m.group(2).strip()


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return list(v)


def load_skill(path: str | Path) -> Skill:
    """加载单个 skill 目录。"""
    path = Path(path).resolve()
    md = path / "SKILL.md"
    if not md.is_file():
        raise SkillError(f"{path} 下没有 SKILL.md")

    raw, body = parse_frontmatter(md.read_text(encoding="utf-8"))

    for required in ("name", "description"):
        if not raw.get(required):
            raise SkillError(f"{md} 的 frontmatter 缺少必填字段 {required!r}")

    name = str(raw["name"]).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
        raise SkillError(f"skill 名 {name!r} 不合法：只允许小写字母、数字、.-_ ")
    if name != path.name:
        raise SkillError(f"skill 名 {name!r} 与目录名 {path.name!r} 不一致，会让路由结果难以追查")

    mode = str(raw.get("mode", MODE_AGENTIC)).strip()
    if mode not in (MODE_AGENTIC, MODE_WORKFLOW):
        raise SkillError(f"skill {name!r} 的 mode 必须是 {MODE_AGENTIC} 或 {MODE_WORKFLOW}")

    unknown = set(raw) - _KNOWN_KEYS
    meta = SkillMeta(
        name=name,
        description=str(raw["description"]).strip(),
        version=str(raw.get("version", "0.1")),
        mode=mode,
        tools=_as_list(raw.get("tools")),
        max_steps=int(raw.get("max_steps", 30)),
        conflicts_with=_as_list(raw.get("conflicts_with")),
        keywords=_as_list(raw.get("keywords")),
        author=str(raw.get("author", "")),
        extra={k: raw[k] for k in unknown},
    )

    workflow: dict[str, Any] | None = None
    wf_path = path / "workflow.yaml"
    if wf_path.is_file():
        try:
            workflow = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise SkillError(f"{wf_path} 解析失败: {e}") from e
    if mode == MODE_WORKFLOW and workflow is None:
        raise SkillError(f"skill {name!r} 声明 mode=workflow 但没有 workflow.yaml")

    return Skill(meta=meta, body=body, path=path, workflow=workflow)


def discover(roots: list[str | Path]) -> list[Skill]:
    """扫描若干根目录下的所有 skill。单个 skill 坏掉不应该拖垮整个注册表。"""
    skills: list[Skill] = []
    errors: list[str] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for md in sorted(root.glob("*/SKILL.md")):
            try:
                skills.append(load_skill(md.parent))
            except SkillError as e:
                errors.append(f"{md.parent.name}: {e}")
    if errors:
        import sys
        for err in errors:
            print(f"[skill-load-error] {err}", file=sys.stderr)
    return skills
