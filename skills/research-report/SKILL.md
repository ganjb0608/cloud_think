---
name: research-report
description: 就一个主题做多源调研并产出带引用的结构化报告。当用户要求"调研"、"研究报告"、
  "竞品分析"、"技术选型对比"、"综述"，或需要汇总多个信息源并交叉验证结论时使用。
  不用于单次事实查询、单篇文章摘要，也不用于代码排障。
version: 1.0
mode: workflow
tools: [web_search, read_file, read_reference, run_script]
max_steps: 30
keywords: [调研, 研究, 报告, 综述, 对比, 选型, research, report, survey]
---

# 调研报告工作流

把一个宽泛主题拆成正交子问题，并行检索，交叉验证，成稿，审稿，按需修订。

## 流程

1. **拆解**（planner）：把主题拆成 3–6 个互不重叠的子问题。子问题之间必须正交，
   否则 researcher 会重复检索同一批资料。
2. **并行调研**（researcher）：每个子问题一个独立实例，各自检索。
   每条结论必须带来源 URL，无来源的推断要显式标注为「推测」。
3. **交叉验证**（dedupe，脚本）：跑 `scripts/dedupe_sources.py` 去掉重复来源，
   并标记数值互相矛盾的结论。这一步是纯代码，不过 LLM。
4. **成稿**（writer）：按 `references/style-guide.md` 的结构写作，全文落盘成 artifact，
   state 里只留引用。
5. **审稿**（reviewer）：按 `references/citation-rules.md` 检查引用完整性并打分。
6. **修订循环**：分数 < 0.8 且修订次数 < 3 时回到第 4 步，否则进入发布。
7. **发布**（publish，脚本）：渲染终稿。发布前会暂停等待人工批准。

## 约束

- 每条结论必须可追溯到具体来源，禁止编造 URL。
- planner 负责保证子问题正交；researcher 不要越界检索别人的子问题。
- 长文一律写进 artifacts，不要堆在状态里——状态只存引用和摘要。
- 能用脚本做的（去重、冲突检测、渲染）不要用 LLM 做。
