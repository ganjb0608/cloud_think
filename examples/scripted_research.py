"""调研任务的脚本化 LLM：不联网、不用 API key 就能跑完整条链路。

测试和 examples/run_demo.py 共用这份数据。真实使用时把 Runtime 的 llm
换成 OllamaClient / AnthropicClient 即可，其余代码一行不用改。
"""
from __future__ import annotations

import re

from cloud_think.llm.base import LLMResponse, Message
from cloud_think.llm.mock import ScriptedLLM, tool_reply


def blob(messages: list[Message]) -> str:
    return "\n".join(m.content for m in messages)


def has_tool_result(messages: list[Message]) -> bool:
    return any(m.role == "tool" for m in messages)


def my_input(messages: list[Message]) -> str:
    """取出 fan-out 实例分到的那份输入。"""
    m = re.search(r"# 你这一份的输入\n(.+)", blob(messages))
    return m.group(1).strip() if m else ""


# --- 调研任务的脚本化产出。刻意让两个子问题给出矛盾数值，用来验证交叉验证步骤 ---
RESEARCH_CLAIMS = {
    "框架生态": {
        "claims": [
            {"text": "2026 年本地推理主要有 vLLM、SGLang、llama.cpp、TensorRT-LLM 四条路线",
             "source": "https://example.org/ecosystem", "value": "", "speculative": False},
            {"text": "llama.cpp 覆盖 CPU 与 Apple Silicon",
             "source": "https://example.org/ecosystem", "value": "", "speculative": False},
        ],
        "sources": ["https://example.org/ecosystem"],
    },
    "性能对比": {
        "claims": [
            {"text": "vLLM 连续批处理吞吐提升", "source": "https://example.org/vllm-bench",
             "value": "2.4x", "speculative": False},
        ],
        "sources": ["https://example.org/vllm-bench"],
    },
    "量化方案": {
        "claims": [
            {"text": "Q4_K_M 把 7B 模型显存压到 4.4GB", "source": "https://example.org/quant",
             "value": "4.4GB", "speculative": False},
            {"text": "vLLM 连续批处理 吞吐 提升", "source": "https://example.org/sglang",
             "value": "3.1x", "speculative": False},
        ],
        "sources": ["https://example.org/quant", "https://example.org/sglang"],
    },
    "部署成本": {
        "claims": [
            {"text": "单张 4090 跑 7B 月度电费约 90 元", "source": "https://example.org/cost",
             "value": "90元", "speculative": False},
            {"text": "运维人力成本常被低估", "source": "https://example.org/cost",
             "value": "", "speculative": True},
        ],
        "sources": ["https://example.org/cost"],
    },
}

SUBQUERIES = list(RESEARCH_CLAIMS)

DRAFT_V0 = """## 摘要
本地推理框架已形成四条主要路线。

## 背景
选型需要同时权衡吞吐、显存和成本。

## 发现
### 框架生态
- 2026 年本地推理主要有 vLLM、SGLang、llama.cpp、TensorRT-LLM 四条路线

### 性能对比
- vLLM 连续批处理吞吐提升 2.4x

## 结论
按场景选型。

## 来源
1. https://example.org/ecosystem
"""

DRAFT_V1 = """## 摘要
本地推理框架已形成四条主要路线，选型取决于硬件与负载形态。

## 背景
选型需要同时权衡吞吐、显存和成本。

## 发现
### 框架生态
- 2026 年本地推理主要有 vLLM、SGLang、llama.cpp、TensorRT-LLM 四条路线 [来源](https://example.org/ecosystem)
- llama.cpp 覆盖 CPU 与 Apple Silicon [来源](https://example.org/ecosystem)

### 性能对比
- vLLM 连续批处理吞吐提升 2.4x [来源](https://example.org/vllm-bench)

### 量化方案
- Q4_K_M 把 7B 模型显存压到 4.4GB [来源](https://example.org/quant)

### 部署成本
- 单张 4090 跑 7B 月度电费约 90 元 [来源](https://example.org/cost)
- 推测：运维人力成本常被低估 [来源](https://example.org/cost)

## 分歧与不确定性
- 连续批处理的吞吐提升存在分歧：2.4x [来源](https://example.org/vllm-bench) 与 3.1x [来源](https://example.org/sglang)

## 结论
吞吐优先选 vLLM，跨平台选 llama.cpp。

## 来源
1. https://example.org/ecosystem
2. https://example.org/vllm-bench
3. https://example.org/quant
4. https://example.org/cost
5. https://example.org/sglang
"""


def build_research_llm() -> ScriptedLLM:
    """按角色脚本化，模拟真实的两轮工具调用行为。"""
    llm = ScriptedLLM()

    def router(messages: list[Message]):
        task = blob(messages).split("用户任务：")[-1]
        if any(k in task for k in ("排查", "报错", "超时", "504", "崩溃", "日志")):
            return {"skills": ["incident-triage"], "confidence": 0.88,
                    "reason": "描述的是线上故障，属于排障"}
        return {"skills": ["research-report"], "confidence": 0.93,
                "reason": "要求多源调研并产出带引用的报告"}

    llm.add(lambda m: "你是一个 skill 路由器" in blob(m), router)
    llm.add(lambda m: "ROLE: planner" in blob(m), {"subqueries": SUBQUERIES})

    def researcher(messages: list[Message]):
        subq = my_input(messages)
        if not has_tool_result(messages):
            return tool_reply("web_search", {"query": subq})    # 第一轮：先检索
        return RESEARCH_CLAIMS.get(subq, {"claims": [], "sources": []})

    llm.add(lambda m: "ROLE: researcher" in blob(m), researcher)

    def writer(messages: list[Message]):
        text = blob(messages)
        has_review = '"issues"' in text or '"score"' in text
        return DRAFT_V1 if has_review else DRAFT_V0

    llm.add(lambda m: "ROLE: writer" in blob(m), writer)

    def reviewer(messages: list[Message]):
        text = blob(messages)
        if not has_tool_result(messages):
            ref = re.search(r'"draft_ref":\s*"([^"]+)"', text)
            return tool_reply("read_file", {"path": ref.group(1) if ref else ""})
        if "draft_v0" in text:
            return {"score": 0.6,
                    "issues": ["发现章节的要点缺少 [来源] 标注",
                               "缺少『分歧与不确定性』章节",
                               "来源章节不完整"],
                    "summary": "引用不完整，需要修订"}
        return {"score": 0.92, "issues": [], "summary": "引用完整，可以发布"}

    llm.add(lambda m: "ROLE: reviewer" in blob(m), reviewer)
    return llm


