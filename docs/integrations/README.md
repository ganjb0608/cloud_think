# 把 cloud_think 接到别的 agent 里

这里的文件是可直接复制的模板。背景和取舍见
[../TUTORIAL.md 第 5 部分](../TUTORIAL.md#第-5-部分能跨-agent-用吗)。

三个层次，按需要选：

| 层次 | 做法 | 状态 |
|---|---|---|
| A 共享知识 | 把 `skills/<name>/` 软链到对方的 skill 目录 | 今天可用，零改动 |
| B 共享执行 | 让对方 agent 跑 `ct` 命令 | 今天可用，零改动 |
| C 标准协议 | 做成 MCP server | 未实现，约 150 行 |

## 层次 A：skill 目录复用

```bash
# Claude Code（用户级）
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/research-report" ~/.claude/skills/research-report

# Claude Code（项目级）
mkdir -p .claude/skills
ln -s "$(pwd)/skills/research-report" .claude/skills/research-report
```

注意：对方 agent 会忽略 `workflow.yaml`，所以 `mode: workflow` 的 skill 在那边
降级成「读 SKILL.md 正文自己发挥」。拿到的是流程知识，拿不到 checkpoint / 并行 /
冲突检测。

## 层次 B：让对方 agent 调用 ct

前置：`pip install -e .`（让 `ct` 进 PATH）+ 配好 LLM 后端（`CT_LLM` 等）。

- **Claude Code** → 复制 `claude-code/deep-task/` 到 `.claude/skills/deep-task/`
- **Codex CLI** → 把 `codex/AGENTS.snippet.md` 的内容并进项目的 `AGENTS.md`
- **Qoder / Cursor / Windsurf / Cline** → 形态和上面两个一样（项目规则文件或自定义
  命令里写清楚何时调用 `ct`）。具体配置入口以各家当前文档为准。

### 建议的项目约定

```
项目根/
├── .ct/
│   ├── state.db      # 所有 run 的状态（跨 agent 共享）
│   └── runs/         # 产物
└── skills/           # 能力包
```

```bash
export CT_DB="$PWD/.ct/state.db"
export CT_RUNS="$PWD/.ct/runs"
export CT_SKILLS="$PWD/skills"     # 注意：CLI 用 --skills，这个变量需自行传入
```

跨 agent 用时建议全部写绝对路径——相对路径是相对于**外层 agent 的工作目录**解析的。

### 一个白送的好处

`ct` 不关心 run 是谁启动的，状态全在 `--db` 那个文件里。所以 Claude Code 里启动、
停在人工卡点的任务，可以在 Codex 里或第二天在终端里 `ct resume` 接着做。
库里已设 `PRAGMA busy_timeout=5000`，多进程共用是预期用法。
