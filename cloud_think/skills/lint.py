"""skill lint：在跑之前把写坏的 skill 挑出来。

系统最大的风险不在引擎，在 skill 的写法。一个 description 写得像介绍而不像
触发条件的 skill，再好的路由也选不中它。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .spec import MODE_WORKFLOW, Skill

TRIGGER_HINTS = ("当", "用于", "适用", "需要", "要求", "不用于", "不适用",
                 "when", "use this", "used for", "do not")


@dataclass
class Finding:
    level: str      # error | warn
    skill: str
    message: str

    def __str__(self) -> str:
        mark = "✗" if self.level == "error" else "!"
        return f"{mark} [{self.skill}] {self.message}"


def lint_skill(skill: Skill) -> list[Finding]:
    out: list[Finding] = []
    m = skill.meta

    # --- description：路由准确率的命门 ---
    if len(m.description) < 25:
        out.append(Finding("warn", m.name,
                           f"description 只有 {len(m.description)} 字，太短，路由容易选错"))
    if not any(h in m.description.lower() for h in TRIGGER_HINTS):
        out.append(Finding("warn", m.name,
                           "description 里没有触发条件（'当…时使用' / '不用于…'），"
                           "读起来像介绍而不像触发条件"))
    if len(m.description) > 600:
        out.append(Finding("warn", m.name, "description 过长，会挤占 L1 索引预算"))

    # --- body ---
    if len(skill.body) < 80:
        out.append(Finding("warn", m.name, "SKILL.md 正文过短，几乎没有可执行的流程指令"))
    if len(skill.body) > 20000:
        out.append(Finding("warn", m.name,
                           f"SKILL.md 正文 {len(skill.body)} 字符，超出 L2 预算，"
                           "把细节挪到 references/ 里按需加载"))

    # --- 引用的资源是否真的存在 ---
    for ref in _referenced_paths(skill):
        if not (skill.path / ref).exists():
            out.append(Finding("error", m.name, f"引用了不存在的资源: {ref}"))

    # --- workflow.yaml ---
    if m.mode == MODE_WORKFLOW:
        wf = skill.workflow or {}
        for key in ("state", "flow"):
            if key not in wf:
                out.append(Finding("error", m.name, f"workflow.yaml 缺少 {key!r} 段"))
        agents = wf.get("agents") or {}
        scripts = wf.get("scripts") or {}
        nodes = set(agents) | set(scripts)
        flow = wf.get("flow") or {}
        entry = flow.get("entry")
        if entry and entry not in nodes:
            out.append(Finding("error", m.name, f"flow.entry={entry!r} 不是已定义的节点"))
        for edge in flow.get("edges") or []:
            for side in ("from", "to"):
                v = edge.get(side)
                if v and v not in nodes and v != "END":
                    out.append(Finding("error", m.name, f"边 {edge} 的 {side}={v!r} 不是已定义的节点"))
        for aname, cfg in agents.items():
            for key in ("prompt",):
                p = (cfg or {}).get(key)
                if p and not (skill.path / p).exists():
                    out.append(Finding("error", m.name, f"agent {aname!r} 的 {key}={p!r} 文件不存在"))
            for ref in (cfg or {}).get("context") or []:
                if not (skill.path / ref).exists():
                    out.append(Finding("error", m.name, f"agent {aname!r} 的 context {ref!r} 不存在"))
        for sname, cfg in scripts.items():
            run = (cfg or {}).get("run")
            if run and not (skill.path / run).exists():
                out.append(Finding("error", m.name, f"script 节点 {sname!r} 的 run={run!r} 不存在"))

    # --- 脚本可解析 ---
    for rel in skill.list_resources("scripts"):
        src = (skill.path / rel).read_text(encoding="utf-8", errors="replace")
        try:
            compile(src, rel, "exec")
        except SyntaxError as e:
            out.append(Finding("error", m.name, f"脚本 {rel} 语法错误: {e}"))

    return out


def _referenced_paths(skill: Skill) -> set[str]:
    """从 SKILL.md 正文里提取形如 `references/x.md`、`scripts/y.py` 的引用。"""
    import re
    pattern = re.compile(r"`((?:references|scripts|prompts|templates)/[\w./-]+)`")
    return set(pattern.findall(skill.body))


def lint_all(skills: list[Skill]) -> list[Finding]:
    out: list[Finding] = []
    seen: dict[str, str] = {}
    for s in skills:
        if s.name in seen:
            out.append(Finding("error", s.name, f"skill 名重复，已存在于 {seen[s.name]}"))
        seen[s.name] = str(s.path)
        out.extend(lint_skill(s))
    return out
