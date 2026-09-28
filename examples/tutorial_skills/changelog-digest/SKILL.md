---
name: changelog-digest
description: 把一批 git 提交记录整理成面向用户的发布说明。当用户给出一串 commit
  信息并要求生成 changelog、release notes、版本说明或更新日志时使用。
  不用于代码审查，也不用于排查故障。
version: 1.0
mode: workflow
tools: []
max_steps: 12
keywords: [changelog, release notes, 发布说明, 更新日志, 版本说明]
---

# 发布说明生成

## 流程

1. **分类**（classifier）：每条提交独立判断它属于哪一类（feature / fix / breaking /
   internal），以及是否需要写进面向用户的说明。internal 类不进正文。
2. **归组**（group，脚本）：按类别聚合、统计数量。纯代码，不过 LLM。
3. **成稿**（writer）：按类别分节写成发布说明，落盘成 artifact。

## 约束

- 每条提交只归一类，拿不准就归 internal，不要硬塞进 feature。
- breaking change 必须单独成节并放在最前面。
- 不要编造提交里没有的内容。
