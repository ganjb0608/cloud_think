---
name: deep-task
description: 把需要长时间、多步骤、可恢复执行的复杂任务交给本地 cloud_think 引擎跑。
  当任务需要并行调研多个子问题、需要中途人工批准、预计要跑十几分钟以上，或者中断了
  重来代价很大时使用。不用于能一两轮对话解决的问题，也不用于普通的读写代码。
---

# 把复杂任务交给 cloud_think

本地装了 cloud_think —— 一个有状态工作流引擎，带 checkpoint、并行子 agent、
人工卡点和崩溃恢复。适合那种"跑很久、断了心疼"的任务。

约定：状态库在 `.ct/state.db`，产物在 `.ct/runs/`，能力包在 `skills/`。

## 步骤

**1. 先看有哪些能力包**

```bash
ct --db .ct/state.db --skills skills skills
```

如果没有合适的能力包，**不要硬套**——直接在对话里做，或者先和用户商量新建一个 skill。

**2. 执行**

```bash
ct --db .ct/state.db --runs .ct/runs --skills skills run "<完整的任务描述>"
```

任务描述要自足：cloud_think 是另一个进程，看不到我们的对话历史。

**3. 看状态**

- `done` → 去第 4 步
- `paused` → 它在等人工批准。把 interrupt 里的内容讲给用户，拿到答复后：
  ```bash
  ct --db .ct/state.db --runs .ct/runs --skills skills resume <run_id> --answer '<JSON>'
  ```
- 失败 → `ct --db .ct/state.db trace <run_id>` 看死在哪一步，修完再 `resume`
  （已完成的超步不会重跑，副作用也不会重复执行）

**4. 汇报**

只把**终稿路径**和**几条要点**带回对话。

⚠️ **不要把 `.ct/runs/<run_id>/artifacts/` 里的中间产物全文读进来。**
cloud_think 存在的意义就是把那些上下文隔离在它自己的进程里；
全 `cat` 一遍等于把好处还回去了。用户想看细节，给路径让他自己开。

## 排查

```bash
ct --db .ct/state.db runs                      # 有哪些 run
ct --db .ct/state.db trace <run_id>            # 时间线
ct --db .ct/state.db trace <run_id> --all      # 含 LLM/工具调用明细
ct --db .ct/state.db inspect <run_id> --step N # 某一步的完整状态
```

改了 prompt 想重试但不想从头跑：`ct fork <run_id> --from-step N`。

## 注意

cloud_think 用自己的 LLM 凭证（`CT_LLM` / `ANTHROPIC_API_KEY` 等），
不借用当前会话的额度。如果报"LLM 后端连不上"，是它自己的后端没配好，
命令行的报错里会列出配法。
