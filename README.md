# cloud_think

本地 **skill 驱动**的多 agent 有状态工作流引擎。

核心想法：把"某类复杂任务该怎么做"从代码里解耦出来，变成本地目录里的 **skill 包**。
新增一类复杂任务 = 新建一个目录，不改一行代码。引擎只负责可靠地把它执行完。

```
一个复杂任务
   ↓ 路由        skills/ 下几十个能力包，只看 description 选
   ↓ 渐进式披露   L1 元数据常驻 → L2 加载 SKILL.md → L3 按需读 references/ 和跑 scripts/
   ↓ 编排        模式 A：orchestrator 自行分派  /  模式 B：workflow.yaml 编译成图
   ↓ 执行        多 subagent 并行，各自隔离上下文，只拿 state 切片
   ↓ 兜底        BSP 超步 + reducer 合并 + SQLite checkpoint + resume / fork
产出
```

> **新手从这里开始**：[docs/TUTORIAL.md](docs/TUTORIAL.md) —— 原理、实现、动手操作、
> 与普通 skill 的对比，以及跨 Claude Code / Codex / Qoder 等 agent 使用的完整说明。
>
> 要接到别的 agent 里：[docs/integrations/](docs/integrations/)（有可直接复制的模板）

## 快速开始

```bash
pip install -e ".[dev]"

python examples/run_demo.py        # 端到端跑一遍复杂任务（不需要 ollama / API key）
python examples/run_tutorial.py    # 教程配套的最小例子
pytest -q                          # 107 项测试，全程离线
```

`run_demo.py` 用脚本化 LLM 完整走一遍调研报告任务：路由 → 拆解 → 4 路并行调研 →
脚本交叉验证 → 成稿 → 审稿打回 → 修订 → 人工批准 → 发布。跑完之后：

```bash
ct --db demo.db --runs demo_runs runs                    # 列出所有 run
ct --db demo.db --runs demo_runs trace <run_id>          # 时间线 + token 统计
ct --db demo.db --runs demo_runs inspect <run_id> --step 3
```

接真实模型只需换掉 LLM client，其余代码不动：

```bash
export CT_LLM=ollama CT_MODEL=qwen2.5:7b     # 或 anthropic / openai 兼容端点
ct --corpus tests/fixtures/corpus.json run "调研一下本地 LLM 推理框架的现状"
ct resume <run_id> --answer '{"approved": true}'
```

## 命令

| 命令 | 作用 |
|---|---|
| `ct skills` | 列出已安装 skill 和 L1 索引预算 |
| `ct lint` | 校验 skill（引用是否存在、description 是否含触发条件、脚本是否可解析） |
| `ct graph <skill>` | 导出编译后的 mermaid 流程图 |
| `ct run "<任务>"` | 路由 → 编译 → 执行 |
| `ct resume <run_id> [--answer JSON]` | 崩溃后恢复，或回答人工介入 |
| `ct fork <run_id> --from-step N` | 从第 N 步分叉重跑（改完 prompt 不用从头跑） |
| `ct inspect <run_id> [--step N]` | 查看某一步的完整状态 |
| `ct trace <run_id>` | 时间线、节点耗时、token 消耗 |

## 写一个 skill

```
skills/my-skill/
├── SKILL.md            # 必需：frontmatter（元数据）+ 正文（流程指令）
├── workflow.yaml       # 可选：声明式工作流图（模式 B）
├── prompts/            # 各 subagent 的角色提示
├── references/         # 深度文档，L3 按需加载
└── scripts/            # 确定性代码，stdin 收 JSON、stdout 吐 JSON
```

`SKILL.md` 的 frontmatter 里，**`description` 是整个系统里最重要的一段文本**——
它是路由的唯一依据。写"什么时候用它 / 什么时候不用"，不要写"它是什么"：

```yaml
---
name: research-report
description: 就一个主题做多源调研并产出带引用的结构化报告。当用户要求"调研"、
  "研究报告"、"竞品分析"、"技术选型对比"时使用。不用于单次事实查询、单篇摘要。
mode: workflow          # workflow（确定性图）| agentic（自行编排）
tools: [web_search, read_file, run_script]
---
```

**建议的演化路径**：新 skill 先写成 `mode: agentic`（只有 markdown，orchestrator
自己摸索），观察 3–5 次真实执行，等流程稳定了再固化成 `workflow.yaml` 换取确定性
和可恢复。一上来就写 yaml 会把流程锁死在错误设计上。

## 架构

```
cloud_think/
├── skills/        Skill 层：loader / registry / router / compiler / lint
├── agents/        编排层：orchestrator（模式 A）、sub_agent（上下文隔离）、tool_loop
├── core/          引擎层：state / graph / engine（BSP 超步）/ checkpoint / events
├── llm/           LLM 客户端：ollama / anthropic / openai 兼容 / 脚本化 mock
├── tools/         工具注册表、脚本沙箱、本地 BM25 检索
├── runtime.py     装配层
└── cli.py
```

### 几个关键设计

**BSP 超步模型**。一个超步 = 取 frontier → 并发执行 → 按 reducer 合并 delta →
路由 → 写 checkpoint。超步之间是全局屏障，所以 checkpoint 天然一致，不存在"半个状态"；
崩溃后 resume 只需重跑最后一个 checkpoint 的 frontier。

**节点只返回增量，不直接改状态**。这是并行安全、可重放、可检查点的前提。
同一超步内多个节点写同一 channel 时按 reducer 折叠；如果该 channel 用的是默认的
`replace`，直接抛 `ConflictError` 逼作者显式声明合并语义——这条检查挡掉了
大部分多 agent 状态竞争 bug。

**上下文隔离才是多 agent 的价值**，不是"并行快"。每个 subagent 只拿到
`state_slice` 声明的 channel、`context_refs` 声明的 reference、白名单内的工具，
输出受 `max_tokens_out` 约束。长文写进 `runs/<run_id>/artifacts/`，state 里只存引用。
测试里断言了单个 subagent 的提示规模有界，不随任务整体规模线性增长。

**副作用幂等**。崩溃恢复会重跑整个超步，纯计算重跑无所谓，但写文件、调外部 API 不能重复。
`ctx.effect(key, fn)` 把结果记在 `effects` 表里，重放时直接复用。

**能用代码做的不用 LLM 做**。去重、格式转换、渲染走 skill 的 `scripts/`，
在沙箱里跑（独立进程 + 超时 + cwd 锁定 + 环境变量白名单 + 输出上限）。
skill 可能来自外部分享，不当可信代码跑。

## 测试

107 项，全程离线（脚本化 LLM + 本地 BM25 语料），覆盖：

- reducer 合并语义与并发写冲突检测
- 表达式求值器的沙箱边界（`__import__`、推导式、lambda 一律拒绝）
- 图编译校验：孤儿节点、悬空边、死胡同节点、非法条件表达式
- 超步调度：fan-out / join=all / 空列表不断链 / 重试 / 超时 / on_error 路由 / 死循环护栏
- 崩溃恢复与副作用幂等（内存和 SQLite 两种实现上都跑一遍）
- 人工介入的暂停与恢复、时间旅行分叉
- skill 加载校验、lint、路由三条路径（显式 / LLM / 关键词兜底）与降级
- 脚本沙箱的超时、非零退出、路径越界、环境变量隔离
- 端到端复杂任务：并行调研、交叉验证、修订循环、人工批准、上下文隔离断言

```bash
pytest -q
pytest tests/test_e2e_research.py -v    # 端到端复杂任务
```
