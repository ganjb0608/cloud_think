"""cloud_think —— 本地 skill 驱动的多 agent 有状态工作流。

分层：
  Skill 层   本地目录里的能力包，定义"某类复杂任务怎么做"（可插拔，不改代码）
  编排层     模式 A（orchestrator 自行分派）/ 模式 B（workflow.yaml 编译成图）
  引擎层     BSP 超步调度 + reducer 合并 + SQLite checkpoint + resume/fork
  运行层     LLM 客户端、工具注册表、脚本沙箱
"""
from .core.checkpoint import MemoryCheckpointer, SQLiteCheckpointer
from .core.engine import Engine, RunResult
from .core.events import ConsoleSink, EventBus, JsonlSink, MemorySink
from .core.graph import END, Graph
from .core.state import StateSchema
from .runtime import Runtime, RuntimeConfig
from .skills import SkillRegistry, SkillRouter, compile_skill, load_skill
from .tools import ToolRegistry, build_registry

__version__ = "0.1.0"
__all__ = [
    "Runtime", "RuntimeConfig", "Engine", "RunResult", "Graph", "END", "StateSchema",
    "SQLiteCheckpointer", "MemoryCheckpointer", "EventBus", "ConsoleSink",
    "JsonlSink", "MemorySink", "SkillRegistry", "SkillRouter", "load_skill",
    "compile_skill", "ToolRegistry", "build_registry",
]
