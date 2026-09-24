#!/usr/bin/env python3
"""去重并交叉验证调研结论。

约定：stdin 收 {"state": {...}, "run_dir": ..., "step": ...}，stdout 吐 delta JSON。
这个约定让脚本既能被引擎当节点调用，也能在命令行单独调试：
    echo '{"state":{"findings":{}}}' | python scripts/dedupe_sources.py
"""
from __future__ import annotations

import json
import re
import sys

_NUM = re.compile(r"\d+(?:\.\d+)?")
_STOP = {"的", "了", "和", "与", "在", "是", "有", "对", "为", "a", "an", "the", "of", "to", "is"}
_WORD = re.compile(r"[a-zA-Z]{2,}|[一-鿿]{2,}")


def keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")} - _STOP


def main() -> None:
    payload = json.load(sys.stdin)
    findings = payload.get("state", {}).get("findings") or {}

    seen_claims: dict[tuple[str, str], str] = {}   # (规范化文本, 数值) -> 首次出现的子问题
    cleaned: dict[str, dict] = {}
    flat: list[dict] = []

    for subq, data in findings.items():
        claims = (data or {}).get("claims") or []
        kept = []
        for c in claims:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            value = (c.get("value") or "").strip()
            # 去重键必须带上数值：文本相同但数值不同恰恰是要检出的矛盾，
            # 只按文本去重会把冲突悄悄抹掉。
            key = (re.sub(r"\s+", "", text.lower()), value)
            if key in seen_claims:
                continue                   # 跨子问题的完全重复结论，丢掉
            seen_claims[key] = subq
            kept.append(c)
            flat.append({**c, "subquery": subq})
        sources = sorted({s for s in ((data or {}).get("sources") or []) if s})
        cleaned[subq] = {"claims": kept, "sources": sources}

    # 交叉验证：关键词高度重合但数值不同 -> 标记为矛盾
    conflicts = []
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            a, b = flat[i], flat[j]
            va = (a.get("value") or "").strip()
            vb = (b.get("value") or "").strip()
            if not va or not vb or va == vb:
                continue
            ka, kb = keywords(a["text"]), keywords(b["text"])
            if not ka or not kb:
                continue
            overlap = len(ka & kb) / min(len(ka), len(kb))
            if overlap >= 0.5 and _NUM.search(va) and _NUM.search(vb):
                conflicts.append({
                    "topic": sorted(ka & kb),
                    "a": {"text": a["text"], "value": va, "source": a.get("source", "")},
                    "b": {"text": b["text"], "value": vb, "source": b.get("source", "")},
                })

    total_sources = len({s for d in cleaned.values() for s in d["sources"]})
    print(json.dumps({
        "findings": cleaned,
        "conflicts": conflicts,
        "_stats": {"claims": len(flat), "sources": total_sources,
                   "dropped": sum(len((d or {}).get("claims") or []) for d in findings.values()) - len(flat)},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
