"""测试装置：脚本化 LLM、本地语料、内存存储。

整套测试不联网、不用 API key——引擎和 skill 层的正确性必须能独立验证，
否则每个 bug 都分不清是引擎问题还是模型问题。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from cloud_think.core.checkpoint import MemoryCheckpointer          # noqa: E402
from cloud_think.core.events import EventBus, MemorySink            # noqa: E402
from cloud_think.skills.registry import SkillRegistry               # noqa: E402
from cloud_think.tools.builtin import LocalCorpusSearch, build_registry  # noqa: E402
from examples.scripted_research import (RESEARCH_CLAIMS, SUBQUERIES,  # noqa: E402
                                        build_research_llm)

FIXTURES = Path(__file__).parent / "fixtures"
SKILLS_DIR = ROOT / "skills"

__all__ = ["RESEARCH_CLAIMS", "SUBQUERIES", "build_research_llm"]


@pytest.fixture
def corpus() -> LocalCorpusSearch:
    return LocalCorpusSearch(path=FIXTURES / "corpus.json")


@pytest.fixture
def tools(corpus, tmp_path):
    return build_registry(search=corpus, workspace=tmp_path)


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry([SKILLS_DIR])


@pytest.fixture
def sink() -> MemorySink:
    return MemorySink()


@pytest.fixture
def bus(sink) -> EventBus:
    return EventBus([sink])


@pytest.fixture
def cp() -> MemoryCheckpointer:
    return MemoryCheckpointer()


@pytest.fixture
def research_llm() -> ScriptedLLM:
    return build_research_llm()
