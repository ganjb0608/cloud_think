<!-- 把下面这段并进项目的 AGENTS.md -->

## 复杂长任务交给 cloud_think

本项目装了 cloud_think（有状态工作流引擎，带 checkpoint、并行子 agent、
人工卡点、崩溃恢复）。以下任务不要在对话里硬跑，交给它：

- 预计跑十几分钟以上
- 需要并行处理多个子任务
- 需要中途人工批准
- 中断了重来代价很大

```bash
ct --db .ct/state.db --runs .ct/runs --skills skills skills        # 看有哪些能力包
ct --db .ct/state.db --runs .ct/runs --skills skills run "<任务>"   # 执行
ct --db .ct/state.db --runs .ct/runs --skills skills resume <run_id> [--answer JSON]
ct --db .ct/state.db trace <run_id>                               # 排查
ct --db .ct/state.db inspect <run_id> --step N
```

规则：

1. 任务描述要自足——cloud_think 是独立进程，看不到对话历史。
2. 状态 `paused` 说明在等人工批准，把 interrupt 内容告知用户，用 `resume --answer` 回答。
3. 失败先 `trace` 看死在哪，修完 `resume`；已完成的超步不会重跑。
4. **只把终稿路径和要点带回对话**，不要把 `.ct/runs/` 里的中间产物全文读进来——
   那是 cloud_think 替你隔离掉的上下文。
5. 没有匹配的能力包就别硬套，直接在对话里做。
