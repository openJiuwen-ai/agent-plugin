# Git History Trace / Git 历史追踪助手

> **一句话定位（zh）**：只读追踪本地 Git 文件的提交历史与行级 blame 归属，可展示指定提交的元数据与有界 diff，不改动工作区。
>
> **One-liner (en)**: Read-only Git file-history tracing and per-line blame attribution, with commit evidence (SHA / author / date) and bounded diffs — the worktree is never touched.

## 核心能力

- **history**：`git log --name-status -M -n <limit>`（可选 `--follow` 跟随重命名），返回 SHA / 作者 / 邮箱 / ISO 日期 / 主题 / 变更类型（A/M/D/R082 old→new）；limit 默认 20、上限 200；`possibly_more` 提示可能还有更多提交。
- **blame**：`git blame --porcelain -L a,b -- <path>`，返回每行的 SHA、作者、邮箱、时间（ISO）、提交摘要、boundary 标记、未提交标记（零 SHA）与截断的内容预览；区间上限 500 行。
- **show_commit**：引用先经 `rev-parse --verify --end-of-options` 解析为完整 SHA 再使用（防选项注入）；`git show --no-patch` 元数据（含 parents / 是否合并提交）；`include_patch` 时 `--no-ext-diff --no-textconv -M` 的有界 diff（8000 字符截断）。
- **安全设计**：file_path 必须是仓库内相对路径（拒绝绝对路径 / `..` 穿越 / `-` 开头 / 控制字符）；blame 前置 `check-attr` 检查自定义 diff 驱动（防 textconv / 外部差异程序执行）；全部命令 `--no-optional-locks --no-pager`；15 秒超时；绝不 checkout / reset / commit / fetch。
- **诚实证据**：结果 notes 显式说明 rename / merge / boundary / 历史简化的局限。

## 组件协作

```
用户请求（这行代码是谁改的？这个提交改了什么？）
   │
   ▼
skills/git-history-tracing ── 取证工作流与证据引用规范
   │
   ▼
tools/git_history_trace_tool.py ── GitHistoryTraceTool
   ├── history ──► git log --name-status [-M] [--follow]
   ├── blame ──► check-attr 守卫 + git blame --porcelain -L
   └── show_commit ──► rev-parse --verify --end-of-options + git show
   （只读命令白名单；无 shell；路径与引用注入防护；输出有界）
```

本插件不包含 rail。前置依赖：本机已安装 git CLI 并在 PATH 中（本插件不安装 git）。

## 目录结构

```
git-history-trace/
├── manifest.json
├── README.md
├── LICENSE                    # Apache-2.0（与仓库根一致）
├── PROVENANCE.md
├── skills/git-history-tracing/SKILL.md
└── tools/git_history_trace_tool.py
```
