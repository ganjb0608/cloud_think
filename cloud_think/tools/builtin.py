"""内置工具：文件读写、本地语料检索、skill 资源访问、脚本执行。"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .registry import Tool, ToolRegistry
from .sandbox import run_script_json

_WORD = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


class LocalCorpusSearch:
    """本地语料检索，实现 BM25。

    ``web_search`` 的可替换实现：默认查本地 JSON 语料（离线可跑、测试可控），
    换成真实搜索引擎只要替换这个类，工具名和返回结构保持不变。
    """

    def __init__(self, corpus: list[dict[str, Any]] | None = None,
                 path: str | Path | None = None, k1: float = 1.5, b: float = 0.75) -> None:
        self.docs: list[dict[str, Any]] = list(corpus or [])
        if path:
            p = Path(path)
            for f in ([p] if p.is_file() else sorted(p.glob("*.json"))):
                data = json.loads(f.read_text(encoding="utf-8"))
                self.docs.extend(data if isinstance(data, list) else [data])
        self.k1, self.b = k1, b
        self._index()

    def _index(self) -> None:
        self._toks = [_tokenize(f"{d.get('title','')} {d.get('text','')}") for d in self.docs]
        self._tf = [Counter(t) for t in self._toks]
        self._len = [len(t) or 1 for t in self._toks]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        self._df: Counter[str] = Counter()
        for toks in self._toks:
            self._df.update(set(toks))
        self._n = max(1, len(self.docs))

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        q = _tokenize(query)
        scored: list[tuple[float, int]] = []
        for i in range(len(self.docs)):
            score = 0.0
            for term in q:
                tf = self._tf[i].get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (self._n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                denom = tf + self.k1 * (1 - self.b + self.b * self._len[i] / self._avg)
                score += idf * tf * (self.k1 + 1) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        out = []
        for score, i in scored[:top_k]:
            d = self.docs[i]
            out.append({"title": d.get("title", ""), "url": d.get("url", ""),
                        "text": d.get("text", ""), "score": round(score, 3)})
        return out


def build_registry(
    search: LocalCorpusSearch | None = None,
    workspace: Path | None = None,
) -> ToolRegistry:
    """组装默认工具集。``workspace`` 限定文件读写的根目录。"""
    reg = ToolRegistry()
    root = (workspace or Path.cwd()).resolve()

    def _safe(path: str, ctx: Any = None) -> Path:
        base = Path(getattr(ctx, "run_dir", root)) if ctx is not None else root
        p = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        for allowed in {root, base.resolve()}:
            if str(p).startswith(str(allowed)):
                return p
        raise PermissionError(f"路径 {p} 超出允许范围 {root}")

    if search is not None:
        reg.add("web_search",
                "检索资料库并返回带来源 URL 的条目。参数 query（检索词）、top_k（返回条数，默认5）。",
                lambda query, top_k=5: search.search(query, int(top_k)),
                {"type": "object",
                 "properties": {"query": {"type": "string", "description": "检索词"},
                                "top_k": {"type": "integer", "description": "返回条数"}},
                 "required": ["query"]})

    reg.add("read_file", "读取一个文本文件的内容。参数 path。",
            lambda path, ctx=None: _safe(path, ctx).read_text(encoding="utf-8")[:100_000],
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})

    def _write(path: str, content: str, ctx: Any = None) -> str:
        p = _safe(path, ctx)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p}（{len(content)} 字符）"

    reg.add("write_file", "把内容写入文件。参数 path、content。", _write,
            {"type": "object",
             "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
             "required": ["path", "content"]})

    def _list(path: str = ".", ctx: Any = None) -> list[str]:
        p = _safe(path, ctx)
        return sorted(str(c.relative_to(p)) for c in p.iterdir())

    reg.add("list_dir", "列出目录内容。参数 path。", _list,
            {"type": "object", "properties": {"path": {"type": "string"}}})

    async def _read_reference(name: str, ctx: Any = None) -> str:
        """L3 按需加载：只有真正需要的 agent 才把这份文档读进上下文。"""
        skill = getattr(ctx, "skill", None)
        if skill is None:
            raise RuntimeError("当前上下文没有绑定 skill，无法读取 reference")
        return skill.read_resource(name)

    reg.add("read_reference",
            "读取当前 skill 的参考文档（references/ 下的文件）。参数 name，例如 'references/style-guide.md'。",
            _read_reference,
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})

    async def _run_script(name: str, payload: dict | None = None, ctx: Any = None) -> Any:
        skill = getattr(ctx, "skill", None)
        if skill is None:
            raise RuntimeError("当前上下文没有绑定 skill，无法执行脚本")
        script = skill.resource_path(name)
        return await run_script_json(script, payload or {},
                                     cwd=Path(getattr(ctx, "run_dir", root)),
                                     allowed_root=skill.path)

    reg.add("run_script",
            "执行当前 skill 的脚本（scripts/ 下的文件），stdin 传 JSON，stdout 收 JSON。参数 name、payload。",
            _run_script,
            {"type": "object",
             "properties": {"name": {"type": "string"}, "payload": {"type": "object"}},
             "required": ["name"]})

    return reg
