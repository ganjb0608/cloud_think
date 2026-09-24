#!/usr/bin/env python3
"""渲染终稿：把草稿、来源清单、分歧、审稿结论拼成可交付的报告。

纯代码，不过 LLM——拼装和格式化是确定性工作，交给模型既慢又不稳。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path


def main() -> None:
    payload = json.load(sys.stdin)
    state = payload.get("state", {})
    run_dir = Path(payload.get("run_dir", "."))

    topic = state.get("topic", "")
    findings = state.get("findings") or {}
    conflicts = state.get("conflicts") or []
    review = state.get("review") or {}
    revision = state.get("revision", 0)
    draft_ref = state.get("draft_ref", "")

    body = ""
    if draft_ref:
        draft = run_dir / draft_ref
        if draft.exists():
            body = draft.read_text(encoding="utf-8")

    sources: list[str] = []
    for data in findings.values():
        for s in (data or {}).get("sources") or []:
            if s not in sources:
                sources.append(s)

    lines = [f"# {topic}", "",
             f"> 生成日期 {date.today().isoformat()} ｜ 修订 {revision} 次 "
             f"｜ 审稿分 {review.get('score', 'n/a')} ｜ 子问题 {len(findings)} 个 "
             f"｜ 来源 {len(sources)} 条", ""]
    lines.append(body.strip() if body else "_（草稿缺失）_")

    if conflicts:
        lines += ["", "## 附录：脚本检出的数值分歧", ""]
        for c in conflicts:
            lines.append(f"- 关于 **{'/'.join(c.get('topic') or [])}**：")
            lines.append(f"  - {c['a']['value']} — {c['a']['text']} <{c['a']['source']}>")
            lines.append(f"  - {c['b']['value']} — {c['b']['text']} <{c['b']['source']}>")

    if sources:
        lines += ["", "## 附录：全部来源", ""]
        lines += [f"{i}. {u}" for i, u in enumerate(sources, 1)]

    if review.get("issues"):
        lines += ["", "## 附录：审稿遗留问题", ""]
        lines += [f"- {i}" for i in review["issues"]]

    out_dir = run_dir / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "report_final.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"report_ref": str(out.relative_to(run_dir))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
