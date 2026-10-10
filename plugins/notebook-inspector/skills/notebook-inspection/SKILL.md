---
name: notebook-inspection
description: 本地 .ipynb 的静态检查与清洗导出。当用户想在不运行 notebook 的情况下了解 cell 结构、执行顺序、历史报错，或需要一份去除 outputs/execution_count 的干净副本时使用；配合 notebook_inspector 工具，全程绝不执行任何 cell。
---

# Notebook 检查（notebook-inspection）

## 目标

不执行任何 cell，仅通过只读解析 .ipynb 的 JSON 回答三件事：notebook 长什么样（cell 大纲）、跑过什么（execution_count 顺序提示）、哪里错了（记录到的 error 输出）；并按需导出清洗副本。

## 工作流

1. 调用 `notebook_inspector`（action=inspect）获取 cell 大纲：类型、行数、execution_count、有界源码/输出预览（图片与 base64 自动跳过并记录在 skipped_mimes）、error 摘要。
2. 结合 `execution_order` 提示判断执行历史：重复计数说明 cell 被多次执行，文档顺序回退说明结果可能过期；提示是启发式的，需向用户说明。
3. 用户需要干净副本时：先确认一个**全新的** output_path（不存在、不等于源文件、父目录存在），再调用 action=export_clean；成功后报告导出路径与清洗的 code cell 数。

## 验收标准

- 全程未执行任何 cell，源 .ipynb 未被修改。
- inspect 结果只含有界预览，无图片 / base64 内容；畸形结构返回 success=false 与明确原因，而非崩溃。
- export_clean 只写入显式指定的新文件；源文件路径或任何已存在路径一律拒绝。
