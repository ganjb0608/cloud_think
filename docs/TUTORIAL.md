# cloud_think 新手入门教程

读完这篇你会知道：它解决什么问题、内部怎么跑起来的、怎么写自己的第一个 skill、
它和你已经在用的「普通 skill」差在哪，以及能不能跨 Claude Code / Codex / Qoder 用。

全程不需要 API key，不需要 ollama —— 第三部分的每一条命令都能直接跑。

---

## 目录

- [第 0 部分：它解决什么问题](#第-0-部分它解决什么问题)
- [第 1 部分：原理](#第-1-部分原理)
- [第 2 部分：实现](#第-2-部分实现)
- [第 3 部分：动手操作](#第-3-部分动手操作)
- [第 4 部分：和普通 skill 的对比](#第-4-部分和普通-skill-的对比)
- [第 5 部分：能跨 agent 用吗](#第-5-部分能跨-agent-用吗)
- [附录：速查](#附录速查)

---

## 第 0 部分：它解决什么问题

先看一个具体场景。你对 agent 说：

> 调研一下 2026 年本地 LLM 推理框架的现状，出一份带引用的报告。

一个普通 agent 会怎么做：搜一下，读几个网页，写出来。看起来能用。但把要求提高一点——
要覆盖 20 个信息源、结论要交叉验证、发布前你要过一眼——就会撞上三件事：

**一、上下文爆炸。** 20 个网页原文轻松 40 万 token。塞不进窗口；就算塞得进，
注意力早就衰减了，模型会开始忽略中间的内容。

**二、中断即全丢。** 跑到第 12 分钟你的网断了 / API 限流 / 进程被杀。
已经读完的 15 个网页、已经写完的草稿，全没了。重来一遍，再烧一遍钱。

**三、并发写混乱。** 你想让 4 个子 agent 并行调研提速。它们同时往「调研结论」里写，
谁覆盖谁取决于谁先返回。结果不可复现，而且你不会立刻发现。

这三件事不是 prompt 写得不好，是**架构缺失**。cloud_think 就是补这三块的：

| 问题 | 对策 |
|---|---|
| 上下文爆炸 | subagent 各自独立上下文 + 长文外置到磁盘，状态里只存引用 |
| 中断即全丢 | 每个超步落一次 checkpoint，`ct resume` 从断点继续，副作用不重复 |
| 并发写混乱 | 状态字段必须声明合并语义（reducer），没声明就并发写 → 直接报错 |

再加上一条贯穿全局的想法：**「某类复杂任务该怎么做」不应该写在代码里，
而应该是磁盘上一个可插拔的目录**。新增一类任务 = 新建一个目录，不改一行代码。
这个目录就是 **skill**。

---

## 第 1 部分：原理

### 1.1 五个核心概念

```
                    ┌─────────────────────────────────────────┐
一个复杂任务  ───▶  │  ① Skill 路由                            │
                    │     看几十个 skill 的 description 选一个  │
                    └──────────────────┬──────────────────────┘
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │  ② 渐进式披露                            │
                    │     L1 元数据常驻 → L2 读 SKILL.md       │
                    │     → L3 按需读 references/ 跑 scripts/  │
                    └──────────────────┬──────────────────────┘
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │  ③ 编排                                  │
                    │     模式 A: orchestrator 自己分派         │
                    │     模式 B: workflow.yaml 编译成图        │
                    └──────────────────┬──────────────────────┘
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │  ④ BSP 超步执行                          │
                    │     并发跑 → reducer 合并 → 写 checkpoint │
                    │     ⑤ 每个 subagent 上下文独立            │
                    └──────────────────┬──────────────────────┘
                                       ▼
                                     产出
```

### 1.2 概念一：Skill —— 把"怎么做"变成数据

一个 skill 就是一个目录：

```
skills/research-report/
├── SKILL.md            # 必需。frontmatter(元数据) + 正文(流程指令)
├── workflow.yaml       # 可选。声明式工作流图
├── prompts/            # 各个角色的提示词
├── references/         # 深度文档，按需加载
└── scripts/            # 确定性代码
```

`SKILL.md` 长这样：

```markdown
---
name: research-report
description: 就一个主题做多源调研并产出带引用的结构化报告。当用户要求"调研"、
  "研究报告"、"竞品分析"、"技术选型对比"时使用。不用于单次事实查询、单篇摘要。
mode: workflow
tools: [web_search, read_file, run_script]
---

# 调研报告工作流

1. **拆解**：把主题拆成 3-6 个互不重叠的子问题
2. **并行调研**：每个子问题一个独立实例，结论必须带来源 URL
...
```

**`description` 是整个系统里最重要的一段文本**，因为它是路由的唯一依据。
写「什么时候用它 / 什么时候不用」，不要写「它是什么」：

```yaml
# ✗ 像产品介绍，路由选不中
description: 一个强大的调研工具，支持多源信息汇总和智能分析。

# ✓ 像触发条件，路由能判断
description: 当用户要求"调研"、"竞品分析"、"技术选型对比"时使用。
  不用于单次事实查询，也不用于代码排障。
```

### 1.3 概念二：状态 + reducer —— 节点不许直接改状态

这是整套设计的地基，值得慢慢看。

工作流的共享状态由若干 **channel** 组成，每个 channel 声明自己的**合并语义**：

```yaml
state:
  topic:      {type: str}                      # 默认 replace（独占）
  findings:   {type: dict, reducer: merge}     # 多个 agent 各写各的 key
  revision:   {type: int,  reducer: add}       # 累加计数
  subqueries: {type: list, reducer: replace}
```

**节点不能直接修改状态，只能返回增量（delta）。** 引擎负责把同一时刻所有节点的
增量按 reducer 折叠进状态：

```
researcher#0 返回 {findings: {"框架生态": {...}}}   ┐
researcher#1 返回 {findings: {"性能对比": {...}}}   ├─ merge ─▶ findings = 四个 key 都有
researcher#2 返回 {findings: {"量化方案": {...}}}   │
researcher#3 返回 {findings: {"部署成本": {...}}}   ┘
```

为什么非要这么绕？因为这是**并行安全、可重放、可检查点**三件事的共同前提：

- **并行安全**：没有"谁先写谁后写"的问题，折叠顺序由 reducer 的结合律保证
- **可重放**：节点是纯函数，同样的输入状态重跑一遍得到同样的增量
- **可检查点**：状态只在超步边界变化，快照必然是一致的

还有一个白送的好处：**并发写冲突会在开发阶段就炸出来**。如果两个节点同时写一个
用默认 `replace` 的 channel，引擎不会随机选一个，而是直接报错：

```
ConflictError: channel 'topic' 在同一超步内被多个节点写入: ['work#0', 'work#1']。
请为该 channel 显式声明 reducer（add/merge/append/extend/last）。
```

这条检查挡掉了多 agent 系统里最难查的一类 bug —— 那种"偶尔结果不对但重跑就好了"的 bug。

### 1.4 概念三：BSP 超步 —— 为什么 checkpoint 一定是一致的

执行模型借用了 Pregel 的 BSP（Bulk Synchronous Parallel）。一个**超步**做五件事：

```
超步 N:
  1. 取出 frontier（这一步要跑哪些节点）
  2. 并发执行它们                        ← 真并行，每个节点有独立超时和重试
  3. 收集所有 delta，按 reducer 合并      ← 冲突在这里被发现
  4. 跑路由函数，算出下一步的 frontier
  5. 原子写 checkpoint(step=N+1)         ← 只有前四步全部完成才写
```

超步之间是**全局屏障**。这带来一个很值钱的性质：

> checkpoint 要么是超步 N 完整结束后的状态，要么根本不存在。
> 不存在"半个状态"。

所以崩溃恢复不需要任何进度推断，就是「重跑最后一个 checkpoint 里的 frontier」。
本项目里 `Checkpoint(step=N)` 存的是**执行超步 N 之前**的状态和待执行的 frontier，
`resume` 就是照着它再跑一遍，逻辑上没有第二种可能。

代价是诚实的：一个慢节点会拖住整个超步。对 agent 工作流（节点耗时都在秒级 LLM 调用）
这个代价可以接受，换来的一致性和可重放性划算得多。

### 1.5 概念四：上下文隔离 —— 多 agent 真正的价值

一个常见误解：多 agent 是为了"并行跑得快"。**不是。** 主要价值是上下文隔离。

```
单 agent：
  主上下文 ─ 网页1全文 ─ 网页2全文 ─ … ─ 网页20全文 ─▶ 40 万 token，废了

多 subagent：
  ┌─ subagent1（独立上下文）读网页1-5 ─▶ 返回 300 字结论 ─┐
  ├─ subagent2（独立上下文）读网页6-10 ─▶ 返回 300 字结论 ─┤
  ├─ subagent3（独立上下文）读网页11-15 ─▶ 返回 300 字结论 ├─▶ 主上下文只多 6k token
  └─ subagent4（独立上下文）读网页16-20 ─▶ 返回 300 字结论 ─┘
```

本项目用三条硬规则强制这件事，不靠提示词自觉：

```yaml
researcher:
  state_slice: [topic]        # ① 只注入这几个 channel，看不到别人的 findings
  context: []                 # ② 只加载声明的参考文档
  tools: [web_search]         # ③ 只能用白名单里的工具
  max_tokens_out: 1200        # ④ 输出受限，逼它精炼
```

再配一条：**长文走磁盘，状态里只存引用**。

```yaml
writer:
  output: {draft_ref: artifact}       # 正文写进 runs/<id>/artifacts/draft_v0.md
  artifact_name: "draft_v{revision}.md"
```

状态里 `draft_ref = "artifacts/draft_v0.md"` 只占 25 个字符。下游的 reviewer
想看正文就自己用 `read_file` 读——读进的是 **它自己的**上下文，不占主上下文。

项目的测试里对这件事下了断言，不是"跑通了"就算：

```python
# tests/test_e2e_research.py
sizes = [e.payload["prompt_tokens"] for e in sink.of("subagent")]
assert max(sizes) < 4000, f"最大提示 {max(sizes)} token，上下文隔离失效"
```

### 1.6 概念五：渐进式披露 —— 复杂任务能跑起来的前提

复杂任务的领域知识量远超上下文窗口。所以 skill 分三层加载：

| 层 | 内容 | 什么时候进上下文 | 规模 |
|---|---|---|---|
| **L1** | 所有 skill 的 `name` + `description` | 启动时，常驻 | ~80 token × N |
| **L2** | 命中 skill 的 `SKILL.md` 正文 | 路由命中后 | < 5k token |
| **L3** | `references/*.md`、`scripts/*` 的执行 | 正文里指引，按需取 | 不限 |

L3 是关键。`references/citation-rules.md` 有 200 行引用细则，但**只有 reviewer 需要它**，
那就只在 reviewer 的上下文里加载：

```yaml
writer:
  context: [references/style-guide.md]      # 只有写作规范
reviewer:
  context: [references/citation-rules.md]   # 只有引用规则
```

这条做对了，128k 窗口能撑住原本需要 500k 的任务。

查一眼 L1 预算花了多少：

```bash
$ ct skills
L1 索引: 2 个 skill，约 131 token（预算 3000）
```

预算控制在 2-3k token 内，大约支撑 30 个 skill 常驻。超了就该上向量召回
（`SkillRouter._keyword_rank` 就是预留的替换点）。

---

## 第 2 部分：实现

### 2.1 分层与目录

```
cloud_think/
├── skills/          Skill 层
│   ├── spec.py        Skill / SkillMeta 数据模型，资源路径越界防护
│   ├── loader.py      frontmatter 解析 + 校验（名字要和目录名一致等）
│   ├── registry.py    注册表、L1 索引、热重载
│   ├── router.py      三路路由：显式 / LLM 语义 / BM25 兜底
│   ├── compiler.py    workflow.yaml -> Graph
│   └── lint.py        skill 校验器
├── agents/          编排层
│   ├── sub_agent.py     上下文隔离的执行体（本项目的主力）
│   ├── orchestrator.py  模式 A 的软编排
│   └── tool_loop.py     节点内的 LLM ↔ 工具微循环
├── core/            引擎层
│   ├── state.py       Channel + Reducer + 冲突检测
│   ├── graph.py       构图 + 编译期校验
│   ├── engine.py      BSP 超步调度器 ← 心脏
│   ├── checkpoint.py  SQLite 持久化
│   ├── context.py     Ctx：节点跟外界唯一的接触面
│   ├── expr.py        受限表达式求值器（条件边用）
│   └── events.py      事件总线
├── llm/             ollama / anthropic / openai兼容 / 脚本化 mock
├── tools/           工具注册表、脚本沙箱、本地 BM25 检索
├── runtime.py       装配层：把上面所有东西接起来
└── cli.py           命令行
```

### 2.2 一个任务在代码里走的路

```
ct run "调研一下…"
  │
  ├─ cli.cmd_run                     解析参数，按 --llm 造 LLM client
  ├─ Runtime.run_task                装配层入口
  │   ├─ SkillRouter.route           → 选中 research-report（记 skill_routed 事件）
  │   ├─ compile_skill               → mode=workflow，读 workflow.yaml
  │   │   └─ compile_workflow        → StateSchema + Graph，编译期全量校验
  │   │       ├─ SubAgentSpec.from_yaml  每个 agents: 条目变成一个角色契约
  │   │       ├─ _script_node            每个 scripts: 条目变成一个沙箱节点
  │   │       └─ Graph.compile           孤儿节点/悬空边/死胡同/非法条件 全在这炸
  │   └─ Engine.invoke
  │       ├─ 存 Checkpoint(step=0)   状态=初始值, frontier=[entry]
  │       └─ Engine._drive           ← 主循环，就是 1.4 节那五步
  │           ├─ _expand             parallel_over 展开成 N 个实例
  │           ├─ _run_task × N       asyncio.gather 并发；各自重试/超时
  │           │   └─ SubAgent.run
  │           │       ├─ build_messages   装配提示：skill正文 + 角色提示
  │           │       │                    + context_refs + state_slice + arg
  │           │       ├─ tool_loop        LLM ↔ 工具循环（不占超步）
  │           │       └─ _parse           按 output 契约解析成 delta
  │           ├─ StateSchema.apply   按 reducer 折叠，冲突则 ConflictError
  │           ├─ _route_all          条件边求值 + join 屏障 → 下一个 frontier
  │           └─ cp.save             原子写 Checkpoint(step=N+1)
  └─ 打印状态 / 产物路径
```

### 2.3 三个值得单独讲的机制

**副作用幂等（`Ctx.effect`）**

崩溃恢复会重跑整个超步。纯计算重跑无所谓，但写文件、调外部 API 不能重复：

```python
# 任何有副作用的操作都套一层
ref = await ctx.effect("write_report", lambda: path.write_text(content))
```

effect key 会被限定成 `{step}:{instance}:{key}` 存进 `effects` 表。重放发生在同一个
step，key 相同 → 直接返回缓存结果，不重新执行。而 writer 第二次修订是在不同的 step，
key 自然不同，不会误命中。

**人工介入（`Ctx.interrupt`）**

```yaml
publish:
  run: scripts/render_report.py
  interrupt_before: true       # 发布前暂停等人工批准
```

机制：`ctx.interrupt(payload)` 抛出信号 → 引擎捕获 → 保持 frontier 不变、
状态标 paused、写 checkpoint → 返回。之后：

```bash
ct resume <run_id> --answer '{"approved": true}'
```

答复存进 `interrupts` 表，节点重放时 `ctx.interrupt` 发现已有答复，直接返回答复而不再抛出。

**注意**：interrupt 之前该节点已执行的部分**会被重放**，所以 interrupt 之前的副作用
必须走 `ctx.effect()`。

**受限表达式（条件边）**

```yaml
- {from: reviewer, to: writer, if: "review.score < 0.8 and revision < 3"}
```

这不是 `eval`。`core/expr.py` 走 AST 白名单，只允许比较、布尔、算术、点号取值和
若干白名单函数（`len`/`min`/`max`/…）。下面这些一律拒绝：

```python
__import__('os').system('ls')   # ExpressionError
open('/etc/passwd').read()      # ExpressionError
[x for x in range(3)]           # ExpressionError
(lambda: 1)()                   # ExpressionError
```

而且引用了未声明的 channel 会在**编译期**报错，不是跑到第 7 个超步才发现。

---

## 第 3 部分：动手操作

### 3.1 装好，零配置跑通

```bash
git clone <repo> && cd cloud_think
pip install -e ".[dev]"

python examples/run_demo.py      # 不需要 ollama，不需要 API key
pytest -q                        # 107 项测试，全程离线
```

`run_demo.py` 用**脚本化 LLM**（`examples/scripted_research.py`）驱动，所以离线可跑。
它会完整走一遍调研任务：

```
方式: llm   选中: ['research-report']
[step_started] step 0 {"nodes": ["planner"]}
[step_started] step 1 {"nodes": ["researcher#0","researcher#1","researcher#2","researcher#3"]}
[script_run]   step 2 dedupe {"script": "scripts/dedupe_sources.py"}
[step_started] step 3 {"nodes": ["writer"]}
[step_started] step 4 {"nodes": ["reviewer"]}      ← 打 0.6 分，打回
[step_started] step 5 {"nodes": ["writer"]}        ← 修订
[step_started] step 6 {"nodes": ["reviewer"]}      ← 0.92 分，通过
[interrupt]    step 7 publish                      ← 停下等人工批准
...
状态 done   超步 8   节点执行 11 次   token in=12298 out=715
```

### 3.2 读懂一次运行

跑完之后这些命令都能用（**注意：这几个只读命令不需要任何 LLM 配置**）：

```bash
ct --db demo.db --runs demo_runs runs                      # 列出所有 run
ct --db demo.db --runs demo_runs trace <run_id>            # 时间线
ct --db demo.db --runs demo_runs trace <run_id> --all      # 含 LLM/工具调用明细
ct --db demo.db --runs demo_runs inspect <run_id> --step 3 # 第 3 步的完整状态
```

`trace` 的输出长这样，每一行是「耗时 / 超步 / 事件 / 节点 / 细节」：

```
   0.00s  s0   run_started
   0.00s  s0   node_finished    planner       {"channels":["subqueries"],"tokens_in":471}
   0.01s  s1   node_finished    researcher#0  {"channels":["findings"],"tokens_in":1151}
   0.01s  s1   node_finished    researcher#1  {"channels":["findings"],"tokens_in":1261}
   0.01s  s1   state_updated                  {"channels":{"findings":["researcher#0",…]}}
   0.02s  s2   script_run       dedupe        {"script":"scripts/dedupe_sources.py"}
   ...
节点执行 11 次；token in=12298 out=715
```

`state_updated` 那行特别有用：它告诉你**这个 channel 是被哪几个节点写的**。
调查"状态怎么变成这样的"就看这一行。

### 3.3 写你的第一个 skill（agentic 模式，5 分钟）

新 skill **不要**一上来写 `workflow.yaml`。流程是摸索出来的，不是设计出来的。
先写成 `mode: agentic`，只有一个 markdown：

```bash
mkdir -p my_skills/pr-summary
cat > my_skills/pr-summary/SKILL.md <<'EOF'
---
name: pr-summary
description: 把一个 PR 的改动整理成给 reviewer 看的摘要。当用户给出 diff 或 PR
  链接并要求"总结改动"、"写 PR 说明"、"帮我 review 前先梳理一下"时使用。
  不用于实际执行 code review，也不用于写 changelog。
mode: agentic
tools: [read_file, list_dir]
max_steps: 10
---

# PR 改动摘要

## 流程
1. **定界**：先看改了哪些文件，按模块分组，判断哪些是主线改动、哪些是连带修改
2. **并行细读**：每个模块派一个子 agent 读具体 diff，各自返回不超过 200 字的要点
3. **汇总**：按「意图 / 主要改动 / 风险点 / reviewer 该重点看哪里」四段写出来

## 约束
- 风险点必须指向具体文件和行为，不接受"可能有兼容性问题"这种空话
- 子 agent 的 diff 原文留在它自己的上下文里，只把结论带回来
EOF

ct --skills my_skills lint      # 先过一遍校验
ct --skills my_skills skills    # 确认被识别到
```

`lint` 会检查：`description` 有没有写触发条件、正文里引用的 `references/xxx.md`
是否真的存在、`scripts/` 里的脚本能不能通过语法检查、名字和目录名是否一致。

现在就能跑了（这一步需要 LLM 后端，见 3.6）：

```bash
ct --skills my_skills run "总结一下 cloud_think/core/engine.py 这次的改动"
```

agentic 模式下，orchestrator 读完你的 SKILL.md，自己决定派几个子 agent、怎么分工。
灵活，但每次路径可能不一样。

### 3.4 升级成 workflow 模式（流程稳定之后）

观察 3-5 次真实执行，流程稳定了，再固化成图换取确定性和可恢复。
本仓库带了一个完整的最小例子，直接照着改：

```bash
python examples/run_tutorial.py                                    # 先跑一遍
ct --skills examples/tutorial_skills graph changelog-digest        # 看编译出的图
cat examples/tutorial_skills/changelog-digest/workflow.yaml        # 看怎么写的
```

这个例子（`changelog-digest`）把一批 git 提交整理成发布说明，3 个节点：

```yaml
task_channel: release          # 任务文本注入到哪个 channel

state:
  release:    {type: str}
  commits:    {type: list}                    # 由 --input 传入
  classified: {type: dict, reducer: merge}    # 多个实例各写各的 key
  buckets:    {type: dict}
  notes_ref:  {type: str}

agents:
  classifier:
    prompt: prompts/classifier.md
    parallel_over: commits       # ← 每条提交一个独立实例，并行判断
    key_by: arg                  # ← 用自己那份输入做归并 key，不依赖模型填对
    output: {classified: json}
    max_tokens_out: 200

  writer:
    prompt: prompts/writer.md
    state_slice: [release, buckets]           # ← 只给它这两个 channel
    output: {notes_ref: artifact}             # ← 长文落盘，状态只存引用
    artifact_name: "RELEASE_NOTES.md"

scripts:
  group:
    run: scripts/group.py        # 纯代码归组，不过 LLM
    reads: [classified]
    writes: [buckets]

flow:
  entry: classifier
  edges:
    - {from: classifier, to: group, join: all}    # ← 等所有实例都跑完
    - {from: group,      to: writer}
    - {from: writer,     to: END}
```

跑起来：

```
状态 done   超步 3
并行分类实例 ['classifier#0', 'classifier#1', 'classifier#2', 'classifier#3']
脚本归组统计 [{'breaking': 1, 'feature': 1, 'fix': 1, 'internal': 1}]
进入正文的类别 ['breaking', 'feature', 'fix']（internal 已被脚本剔除）
```

注意最后那行体现的一个原则：**能用代码保证的事情不要交给模型的自觉**。
`internal` 类不该进发布说明——所以 `group.py` 直接把它剔掉，而不是塞给 writer
再用提示词让它"记得忽略"。

**脚本节点的约定**（很简单，所以脚本也能单独调试）：

```python
# stdin 收 {"state": {...}, "arg": ..., "run_dir": "...", "step": N}
# stdout 吐 delta JSON
state = json.load(sys.stdin)["state"]
print(json.dumps({"buckets": {...}, "_stats": {...}}))
#                                   ^^^^^^ 下划线开头的 key 是诊断信息，
#                                          打到事件里，不写进状态
```

```bash
# 单独调试，不用起整个工作流
echo '{"state":{"classified":{}}}' | python examples/tutorial_skills/changelog-digest/scripts/group.py
```

### 3.5 workflow.yaml 全部可用的键

**`agents:` 下每个角色**

| 键 | 作用 |
|---|---|
| `prompt` | 角色提示词文件（skill 内相对路径） |
| `state_slice` | 注入哪些 channel。**不写 = 不注入任何状态** |
| `context` | 加载哪些 reference（L3 按需加载） |
| `tools` | 工具白名单。不写 = 没有工具 |
| `output` | `{channel: json\|text\|artifact}` |
| `artifact_name` | artifact 文件名，支持 `{revision}` 之类的状态插值 |
| `const` | 固定增量，如 `{revision: 1}`（用于 artifact 输出没法同时吐 JSON 的情况）|
| `key_by` | `arg` = 用 fan-out 的输入做归并 key |
| `parallel_over` | 按哪个 list channel 做 fan-out |
| `max_tokens_out` | 输出预算，超了记警告事件 |
| `max_tool_rounds` | 节点内工具循环上限（默认 8） |
| `retry` | `{max_attempts, base, factor, max_delay, jitter}` |
| `timeout` | 节点超时秒数 |
| `max_visits` | 单节点最多被访问几次（防反思循环打转，默认 25） |
| `interrupt_before` / `interrupt_after` | 人工卡点 |
| `on_error` | 失败时路由到哪个节点，而不是整个 run 失败 |
| `temperature` | 采样温度 |

**`scripts:` 下每个脚本节点**：`run`、`reads`、`writes`、`timeout`、`retry`、
`join`、`interrupt_before`、`on_error`

**`flow:`**：`entry` + `edges`，每条边 `{from, to, if, join, label}`

**路由语义**（单个节点的出边）：带条件的边按声明顺序检查、**第一条为真的胜出**；
都不为真时，所有无条件边**全部**触发（这就是静态 fan-out）。

**`state:` 可用的 reducer**：`replace`(默认，独占)、`last`、`add`、`merge`、
`append`、`extend`、`union`、`max`

### 3.6 接真实模型

```bash
# 本地 ollama
ollama serve && ollama pull qwen2.5:7b
export CT_LLM=ollama CT_MODEL=qwen2.5:7b

# 或 Claude API
export CT_LLM=anthropic CT_MODEL=claude-sonnet-5 ANTHROPIC_API_KEY=sk-...

# 或任何 OpenAI 兼容端点（vLLM / LM Studio / llama.cpp server / 网关）
export CT_LLM=openai OPENAI_BASE_URL=http://127.0.0.1:8000/v1 CT_MODEL=local

ct --corpus tests/fixtures/corpus.json run "调研一下本地 LLM 推理框架的现状"
```

一个实际建议：**路由和审稿这类短任务跑本地小模型，写作和推理跑大模型**。
LLM 层是 Protocol，同一个工作流里换不同后端不需要改工作流定义。

### 3.7 人工卡点、崩溃恢复、分叉调试

```bash
# 跑到人工卡点会停下，状态是 paused
ct run "调研…"
#   状态 paused
#   等待人工输入: {"node": "publish", "payload": {...}}
#   回答后继续:  ct resume run_abc123 --answer '"yes"'

ct resume run_abc123 --answer '{"approved": true}'

# 进程崩了/被杀了，直接接着跑（副作用不会重复执行）
ct resume run_abc123

# 改完 prompt 不想从头跑：从第 3 步分叉
ct fork run_abc123 --from-step 3
ct fork run_abc123 --from-step 3 --set topic='"换个主题"'
```

`fork` 是调试 agent 工作流最省时间的能力。第 5 步的 writer 产出不满意，
改完 `prompts/writer.md` 之后从第 5 步分叉，前面 4 步的调研结果直接复用，
不用重新检索 20 个网页。

### 3.8 常见问题排查

| 现象 | 原因 / 怎么办 |
|---|---|
| `LLM 后端连不上` | 没配后端。CLI 会直接给出三种配法，或用 `python examples/run_demo.py` 零配置看流程 |
| `没有匹配的 skill` | `description` 没写触发条件。跑 `ct lint`；或用 `--skill <name>` 跳过路由 |
| `ConflictError: 同一超步内被多个节点写入` | fan-out 的多个实例写了同一个 `replace` channel。改成 `merge`，或者加 `key_by: arg` |
| `这些节点从 entry 不可达` | `flow.edges` 漏了一条边 |
| `这些节点无法到达 END` | 有死胡同节点。注意环是允许的，只要存在通往 END 的路径 |
| `表达式引用了未声明的 channel` | `if:` 里写的名字不在 `state:` 段里（编译期就会报） |
| `节点访问次数超过 max_visits` | 反思循环没收敛。检查退出条件，或调大 `max_visits` |
| `超过 max_steps` | 图里有停不下来的环。条件边的退出条件永远不为真 |
| `脚本写了未声明的 channel` | 脚本 stdout 的 key 要么在 `writes:` 里声明，要么用 `_` 开头当诊断 |
| `脚本的 stdout 不是合法 JSON` | 脚本里有 `print()` 调试输出。调试信息要打到 stderr |
| 任务失败了但不知道死在哪 | `ct trace <run_id> --all`，堆栈存在事件表里 |
| `ct fork` / `ct resume` 也要连 LLM | 已知限制：这两个命令会构造 LLM client。`inspect`/`trace`/`runs`/`skills`/`lint`/`graph` 不需要 |

---

## 第 4 部分：和普通 skill 的对比

### 4.1 先说相同的部分

**格式是一样的。** cloud_think 的 skill 格式刻意对齐 Anthropic Agent Skills 规范：
`SKILL.md` + YAML frontmatter + 同目录资源。所以同一个 skill 目录可以：

- 放进 `.claude/skills/` → Claude Code 原生识别
- 交给 cloud_think → 引擎按 `workflow.yaml` 确定性执行

这不是巧合，是为了让 skill 在两边通用，不用维护两份。

### 4.2 再说不同的部分

**差别不在格式，在执行保障。**

| | 普通 skill（agent 原生） | cloud_think skill |
|---|---|---|
| **存在形式** | `SKILL.md` 目录 | 同样是 `SKILL.md` 目录 |
| **谁执行** | agent 读完自己发挥 | 引擎按图确定性执行（或 orchestrator 软编排） |
| **状态在哪** | 全在对话上下文里 | 外置到 SQLite + 磁盘 artifacts，状态只存引用 |
| **中断怎么办** | 对话断了就全丢，从头再来 | 超步级 checkpoint，`ct resume` 从断点继续 |
| **并行** | 靠 agent 自己决定，通常是串行 | `parallel_over` 声明式 fan-out，超步内真并发 |
| **并发写冲突** | 无此概念（单上下文） | reducer 显式声明，没声明就并发写 → 编译/运行期报错 |
| **上下文预算** | 随任务规模膨胀，长任务必然爆 | subagent 隔离 + 引用外置，主上下文有界（有测试断言）|
| **副作用重复** | 重试就重复执行 | `ctx.effect` 幂等，恢复不重做 |
| **人工介入** | 靠对话轮次，关掉窗口就没了 | interrupt 持久化，可以隔一天再回答 |
| **可观测** | 翻对话记录 | 事件表 + `ct trace` + 每节点 token 成本 |
| **调试迭代** | 改 prompt 后整个任务重跑 | `ct fork --from-step N` 从任意步分叉 |
| **重跑成本** | 重跑 = 重新烧一遍钱 | 恢复复用已完成的超步 |
| **确定性** | 每次路径可能不同 | 模式 B 下路径固定，可复现 |
| **审计** | 无 | 每个节点每次尝试都有记录（`node_runs` 表） |

### 4.3 什么时候**不该**用 cloud_think

这点比上面那张表更重要：

- **单次问答、一轮就能完事的任务** —— 套一层引擎纯属自找麻烦，用普通 skill
- **探索性任务，你自己都不知道要几步** —— 先用普通 skill 或 agentic 模式摸
- **流程还没稳定** —— 一上来写 `workflow.yaml` 会把流程锁死在错误设计上
- **任务跑不到几分钟** —— checkpoint/resume 的价值出不来，白付复杂度
- **需要人在环里频繁来回讨论** —— 那是对话的强项，不是工作流的强项

判断标准很简单：**任务会不会跑到"中途断了我会心疼"的程度？**
不会，就用普通 skill。会，才值得上引擎。

### 4.4 推荐的演化路径

```
普通 skill（纯 markdown，在 agent 里手工跑）
    │  跑了几次发现：上下文老是爆 / 断了要重来 / 想并行
    ▼
cloud_think agentic 模式（同一个 SKILL.md，加个 mode: agentic）
    │  观察 3-5 次真实执行，流程稳定下来了
    ▼
cloud_think workflow 模式（加 workflow.yaml，换确定性和可恢复）
```

每一步都是加东西，不用重写。这是设计时刻意保留的路径。

---

## 第 5 部分：能跨 agent 用吗

能，但先要分清三个完全不同层次的"跨"。**混在一起谈会得出错误结论。**

```
层次 A：共享知识    skill 目录给别的 agent 读    ← 今天可用，零改动
层次 B：共享执行    别的 agent 调用 ct 命令      ← 今天可用，零改动
层次 C：标准协议    做成 MCP server             ← 需要新增约 150 行
```

### 5.1 层次 A：skill 包跨 agent 复用（只共享知识）

skill 就是磁盘上的文件，任何能读文件的 agent 都能用。

**Claude Code**：格式原生兼容，软链过去就行。

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/research-report" ~/.claude/skills/research-report
# 或者项目级
ln -s "$(pwd)/skills/research-report" .claude/skills/research-report
```

**Codex / Qoder / Cursor / Cline 等**：把目录路径告诉它，或者在它的项目约定文件
（Codex 是 `AGENTS.md`，各家不同）里写一句"复杂调研任务参照 `skills/research-report/SKILL.md`"。

**你得到什么、失去什么，要想清楚：**

| | 得到 | 失去 |
|---|---|---|
| | 流程知识、角色提示、参考文档、可执行脚本 | checkpoint、resume、并行超步、冲突检测、上下文隔离强制、成本统计 |

`workflow.yaml` 是 cloud_think 专有的，别的 agent 会忽略它。所以一个
`mode: workflow` 的 skill 在别的 agent 里会**降级成"读 SKILL.md 正文然后自己发挥"**。

这不算坏事——本项目刻意让 `SKILL.md` 正文和 `workflow.yaml` **描述同一个流程**，
一份给人和 agent 读，一份给引擎执行。但这是**真实的维护负担**：改了流程要记得改两处。
我的建议是把正文当唯一事实来源，yaml 当它的可执行翻译，改 yaml 时顺手对一下正文。

### 5.2 层次 B：把 cloud_think 当成外层 agent 的一个工具（今天就能用）

这是**现在最实用的跨 agent 方式，一行代码都不用改**。

原理很简单：几乎所有 coding agent 都能跑终端命令，而 `ct` 就是个终端命令。

```
┌──────────────────────┐
│  Claude Code / Codex │   ← 外层 agent，负责跟你对话
│  / Qoder / Cursor    │
└──────────┬───────────┘
           │ 跑一条 shell 命令
           ▼
      ct run "复杂任务…"       ← cloud_think 在自己的进程里跑
           │                     带自己的 checkpoint、并行、artifacts
           ▼
  只把"终稿路径 + 要点"返回给外层 agent
  ← 那 40 万 token 的中间产物根本没进外层 agent 的上下文
```

**这个组合的价值**：外层 agent 的上下文保持很小。它发一条命令，拿回一个路径。
所有重活在 cloud_think 的进程里发生，还带崩溃恢复。

#### Claude Code 接法

在 `.claude/skills/deep-task/SKILL.md` 放一个包装 skill（本仓库
`docs/integrations/claude-code/` 下有可直接复制的版本）：

```markdown
---
name: deep-task
description: 把需要长时间、多步骤、可恢复执行的复杂任务交给本地 cloud_think 引擎跑。
  当任务需要并行调研多个子问题、需要中途人工批准、或预计要跑十几分钟以上时使用。
  不用于能一两轮对话解决的问题。
---

# 把复杂任务交给 cloud_think

1. 先看有哪些能力包：`ct --db .ct/state.db skills`
2. 执行：`ct --db .ct/state.db --runs .ct/runs run "<完整任务描述>"`
3. 输出里会有 run_id。如果状态是 `paused`，说明它在等人工批准：
   把 interrupt 的内容念给用户，拿到答复后
   `ct --db .ct/state.db resume <run_id> --answer '<JSON>'`
4. 完成后**只把终稿路径和几条要点带回对话**。不要把 artifacts 里的中间产物
   全文读进来——那正是 cloud_think 替你隔离掉的上下文。
5. 出问题看 `ct --db .ct/state.db trace <run_id>`。
```

第 4 条是重点。如果外层 agent 把所有 artifacts 都 `cat` 一遍，
上下文隔离的好处就全还回去了。

#### Codex CLI 接法

Codex 也能跑 shell。在项目的 `AGENTS.md` 里写清楚什么时候该用它：

```markdown
## 复杂长任务

预计超过 10 分钟、需要并行子任务、或需要中途人工批准的任务，不要在对话里硬跑，
交给本地 cloud_think：

    ct --db .ct/state.db --runs .ct/runs skills          # 看有哪些能力包
    ct --db .ct/state.db --runs .ct/runs run "<任务>"     # 执行
    ct --db .ct/state.db resume <run_id>                 # 中断后继续
    ct --db .ct/state.db trace <run_id>                  # 排查

只把最终产物的路径带回对话，中间产物留在 .ct/runs/ 里。
```

#### Qoder / Cursor / Windsurf / Cline 等 IDE 内 agent

这几家的共同点：都能跑终端命令，大多也支持 MCP。**接入形态就是上面两种之一**，
区别只在配置入口叫什么名字（项目规则文件 / 自定义命令 / MCP 配置）。
具体入口以各家当前文档为准——这些产品的配置格式变动比较频繁，我不在这里写死。

不变的是这条判断：**只要那个 agent 能跑 shell 命令，层次 B 就能用。**

#### 一个跨 agent 的白送好处：run 是共享的

`ct` 的状态全在 `--db` 指定的那个 SQLite 文件里，**它不关心是谁启动的 run**。所以：

```bash
# Claude Code 里启动，跑到人工卡点停下
ct --db .ct/state.db run "调研…"      →  run_abc123, paused

# 换到 Codex 里，或者第二天你自己在终端里
ct --db .ct/state.db resume run_abc123 --answer '{"approved": true}'
```

一个 agent 开的任务，另一个 agent 能接着做。数据库里已经加了
`PRAGMA busy_timeout=5000`，多个进程共用同一个库是预期用法。

### 5.3 层次 B 的三个诚实限制

这几条一定要说清楚，否则你会在生产里踩到：

**一、cloud_think 用的是自己的 LLM 凭证，不是外层 agent 的。**

Claude Code 调 `ct run` 的时候，cloud_think 会用 `CT_LLM` / `ANTHROPIC_API_KEY`
发**它自己的** API 请求。它不会（也没办法）借用 Claude Code 的额度。所以：

- 你要单独给 cloud_think 配后端
- 两边的模型是分开配的，可以故意配不一样（外层用大模型对话，里层用本地小模型跑量）
- 两份账单

**二、沙箱环境里出网可能被挡。**

如果外层 agent 跑在受限容器里（比如 Claude Code 网页版的云容器），
cloud_think 的出网请求同样受那个容器的网络策略约束。这种场景下让它连本机
ollama 通常更稳。

**三、`ct` 得在 PATH 里，工作目录要对。**

`pip install -e .` 之后 `ct` 才可用；`--skills` / `--db` / `--runs` 的相对路径
是相对于**外层 agent 的工作目录**解析的。跨 agent 用的时候建议全写成绝对路径，
或者统一约定放在项目下的 `.ct/`。

### 5.4 层次 C：做成 MCP server（最通用，还没实现）

MCP（Model Context Protocol）是这件事真正的互操作标准。Claude Code、Claude Desktop、
Cursor 等都支持挂 MCP server，越来越多的 agent 也在支持。

**为什么 MCP 比 shell 调用好：**

| | shell 调用（层次 B） | MCP server（层次 C） |
|---|---|---|
| 输入输出 | agent 要解析 stdout 文本 | 结构化 JSON，有 schema |
| 发现能力 | agent 得先知道 `ct` 存在 | 工具列表自动暴露给模型 |
| 长任务 | 命令阻塞到跑完 | 可以立刻返回 run_id，之后轮询 |
| 适用范围 | 需要 agent 有 shell 权限 | 不给 shell 权限的 agent 也能用 |
| 人工卡点 | agent 得自己想到去 resume | `resume_run` 就在工具列表里，模型看得见 |

**要暴露的工具**（直接映射现有的 `Runtime` 方法，所以工作量不大）：

```
list_skills()                        → SkillRegistry.names() + description
run_task(task, inputs?, skill?)      → Runtime.run_task，返回 {run_id, status, interrupt?}
resume_run(run_id, answer?)          → Runtime.resume
inspect_run(run_id, step?)           → Checkpointer.load_at
trace_run(run_id)                    → Checkpointer.events
fork_run(run_id, from_step, set?)    → Runtime.fork
```

配置的形状（大多数 MCP 客户端都是这个形状，**具体文件位置以各客户端文档为准**）：

```json
{
  "mcpServers": {
    "cloud-think": {
      "command": "python",
      "args": ["-m", "cloud_think.mcp"],
      "env": {
        "CT_LLM": "ollama",
        "CT_MODEL": "qwen2.5:7b",
        "CT_DB": "/abs/path/.ct/state.db",
        "CT_RUNS": "/abs/path/.ct/runs",
        "CT_SKILLS": "/abs/path/skills"
      }
    }
  }
}
```

**这部分还没写**。估计约 150 行（一个 MCP server 骨架 + 六个工具的参数 schema +
把 `Runtime` 的返回值序列化成 JSON）。现有的 `runtime.py` 已经是干净的装配层，
所以这层是薄的。需要的话我可以补上。

### 5.5 一句话总结跨 agent 这件事

> **skill 目录是跨 agent 的（层次 A，今天可用）；
> 执行能力通过 shell 命令跨 agent（层次 B，今天可用，且 run 状态天然共享）；
> 想要结构化、能被模型自动发现的接入，需要补一个 MCP adapter（层次 C，约 150 行）。**

---

## 附录：速查

### 命令

```bash
# 不需要 LLM 配置
ct skills                            # 列出 skill + L1 预算
ct lint                              # 校验 skill
ct graph <skill>                     # 导出 mermaid 图 + state 定义
ct runs                              # 列出所有 run
ct inspect <run_id> [--step N]       # 某一步的完整状态
ct trace <run_id> [--all]            # 时间线 + token 统计

# 需要 LLM 配置
ct run "<任务>" [--skill NAME] [--input k=v]
ct resume <run_id> [--answer JSON]
ct fork <run_id> --from-step N [--set k=v]

# 全局选项
--skills DIR    skill 目录（可多次）      --db PATH     SQLite 文件
--runs DIR      产物目录                  --llm KIND    ollama|anthropic|openai
--model NAME    模型名                    --corpus PATH 本地检索语料
-v              打印 LLM/工具调用明细
```

### 环境变量

`CT_LLM`、`CT_MODEL`、`CT_DB`、`CT_RUNS`、`OLLAMA_HOST`、
`ANTHROPIC_API_KEY`、`OPENAI_BASE_URL`

### 术语

| 术语 | 含义 |
|---|---|
| **skill** | 一个目录，装着"某类复杂任务该怎么做"的知识和资源 |
| **channel** | 状态里的一个字段，带合并语义 |
| **reducer** | 同一超步内多个节点写同一 channel 时怎么合并 |
| **delta** | 节点返回的状态增量（节点不能直接改状态） |
| **超步 / superstep** | 一轮"并发执行 → 合并 → 路由 → 存档" |
| **frontier** | 下一个超步要执行的节点集合 |
| **checkpoint** | 某个超步执行**之前**的状态 + frontier 快照 |
| **fan-out** | 一个节点按 list channel 展开成 N 个并行实例 |
| **join=all** | 等所有上游到达才触发（屏障） |
| **artifact** | 落在 `runs/<id>/artifacts/` 的产物，状态里只存引用 |
| **effect** | 有副作用的操作，按 key 记录，重放时不重复执行 |
| **interrupt** | 人工卡点，持久化，可跨进程回答 |
| **L1/L2/L3** | 渐进式披露的三层加载 |

### 相关文件

| 想看什么 | 看哪里 |
|---|---|
| 完整的 workflow 例子 | `skills/research-report/workflow.yaml` |
| 最小的 workflow 例子 | `examples/tutorial_skills/changelog-digest/` |
| agentic 模式例子 | `skills/incident-triage/SKILL.md` |
| 离线跑通复杂任务 | `examples/run_demo.py` |
| 教程配套例子 | `examples/run_tutorial.py` |
| 超步调度器 | `cloud_think/core/engine.py` 的 `_drive` |
| 上下文隔离怎么实现的 | `cloud_think/agents/sub_agent.py` 的 `build_messages` |
| 端到端断言 | `tests/test_e2e_research.py` |
