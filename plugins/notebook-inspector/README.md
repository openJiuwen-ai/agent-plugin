# Notebook Inspector / Notebook 检查器

> **一句话定位（zh）**：不执行任何 cell，静态检查本地 .ipynb——cell 大纲、有界预览、历史错误与执行顺序提示，并可导出去除 outputs/execution_count 的清洗副本。
>
> **One-liner (en)**: Statically inspect local .ipynb files — cell outline, bounded previews, recorded errors and execution-order hints — without executing anything, and export cleaned copies with outputs/execution_count removed.

## 核心能力

- **只读检查（action=inspect）**：解析 .ipynb JSON，返回 cell 类型 / 行数 / execution_count 大纲、源码与文本输出的有界预览（默认每 cell 200 字符，图片与 base64 自动跳过并记录 skipped_mimes）、error 输出摘要，以及 execution_order 顺序提示（重复计数、文档顺序回退、未执行却有输出）。
- **安全边界**：输入文件上限 20MB；最多列出 500 个 cell；畸形 JSON / 非法结构返回明确错误而非崩溃；**绝不执行任何 cell**。
- **清洗导出（action=export_clean）**：移除 code cell 的 `outputs` 与 `execution_count`，其余内容原样保留，仅写入显式指定的全新 `output_path`（独占创建模式），**绝不覆盖源文件或任何已存在文件**。

## 组件协作

```
用户请求（检查 / 排错 / 导出干净副本）
   │
   ▼
skills/notebook-inspection ── 何时检查、如何解读顺序提示、如何验收
   │
   ▼
tools/notebook_inspector_tool.py ── NotebookInspectorTool
   ├── action=inspect ──────► 只读解析 .ipynb ──► 大纲 / 有界预览 / 错误 / 顺序提示
   └── action=export_clean ─► 移除 outputs / execution_count ──► 全新 output_path
```

本插件不包含 rail（无跨调用状态需求）。

## 目录结构

```
notebook-inspector/
├── manifest.json
├── README.md
├── LICENSE                    # Apache-2.0（与仓库根一致）
├── PROVENANCE.md
├── skills/notebook-inspection/SKILL.md
└── tools/notebook_inspector_tool.py
```
