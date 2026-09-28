#!/usr/bin/env python3
"""按类别聚合提交。stdin 收 {"state": {...}}，stdout 吐 delta JSON。"""
import json
import sys

ORDER = ["breaking", "feature", "fix", "internal"]


def main() -> None:
    state = json.load(sys.stdin).get("state", {})
    classified = state.get("classified") or {}

    buckets = {k: [] for k in ORDER}
    for commit, info in classified.items():
        kind = (info or {}).get("kind", "internal")
        if kind not in buckets:
            kind = "internal"
        if not (info or {}).get("user_facing", True) and kind != "breaking":
            kind = "internal"
        buckets[kind].append({"commit": commit,
                              "summary": (info or {}).get("summary", commit)})

    # internal 不进面向用户的说明，所以这里直接剔掉，而不是塞给 writer
    # 再用提示词让它忽略——能用代码保证的事情不要交给模型的自觉。
    print(json.dumps({
        "buckets": {k: buckets[k] for k in ORDER if k != "internal" and buckets[k]},
        "_stats": {k: len(v) for k, v in buckets.items()},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
