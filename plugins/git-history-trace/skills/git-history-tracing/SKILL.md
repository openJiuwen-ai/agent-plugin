---
name: git-history-tracing
description: 本地 Git 仓库的只读历史追踪。当用户想知道某文件 / 某几行代码是谁、何时、在哪个提交里改的，或需要某个提交的证据（SHA / 作者 / 日期 / diff）时使用；配合 git_history_trace 工具，绝不 checkout / reset / commit / fetch。
---

# Git 历史追踪（git-history-tracing）

## 目标

以可复核的证据回答「这段代码从哪来」：文件的提交历史、行级 blame 归属、单个提交的元数据与有界 diff。与 commit-draft（起草提交）和代码审查类能力不同，本技能只做历史取证，不生成草稿或评审意见。

## 工作流

1. `git_history_trace` action=history：拿到文件提交列表（SHA / 作者 / 日期 / 变更类型）；需要跨重命名时加 follow_renames=true，并阅读 notes 中的重命名局限。
2. action=blame：按行区间（start_line/end_line）定位每行的 SHA、作者、时间与提交摘要；未提交修改会标记 uncommitted。
3. action=show_commit：用分支名 / 标签 / SHA / HEAD~n 定位提交（内部先解析为完整 SHA），include_patch=true 附有界 diff。
4. 结论引用证据时给出 SHA 前 12 位 + 作者 + 日期；rename / merge / boundary 的局限按结果 notes 如实转述，不过度断言。

## 验收标准

- 全程只读：`git status --porcelain` 前后一致，无 checkout/reset/commit/fetch。
- 路径只接受仓库内相对路径；穿越（../）与选项注入（- 开头）一律拒绝。
- blame 前自定义 diff 驱动检查命中即拒绝（防 textconv/外部程序执行）。
