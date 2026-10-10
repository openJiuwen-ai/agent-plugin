# SQLite Inspector / SQLite 检查助手

> **一句话定位（zh）**：只读检查本地 SQLite 数据库文件——表清单、列 / 索引 / 外键结构、带参数的限量 SELECT 与执行计划，四重防护确保绝不写入。
>
> **One-liner (en)**: Read-only inspection of local SQLite database files — tables, columns, indexes, foreign keys, bounded parameterized SELECTs with query plans — with four layers of write protection.

## 核心能力

- **list_tables**：列出表与视图（可选每表行数统计，受资源上限约束；过滤 `sqlite_` 内部表）。
- **describe_table**：列定义（类型 / 非空 / 默认值 / 主键）、索引（唯一性 / 来源 / 覆盖列）、外键、建表语句（截断预览）。
- **query**：单条 `SELECT` / `WITH` / `VALUES` + 位置或命名参数；自动附 `EXPLAIN QUERY PLAN`；行数（默认 50，上限 1000）、单元格字符（4096）、总字符（64K）三重截断并显式标注 `truncated`。
- **四重只读防护**：
  1. URI `mode=ro` 只读打开（文件不存在直接报错，**绝不创建新库**）；
  2. `PRAGMA query_only = ON`；
  3. sqlite3 authorizer 拒绝 INSERT/UPDATE/DELETE/DDL/ATTACH/PRAGMA 改写/`load_extension`（不依赖 SQL 前缀检查）；
  4. 进度处理器限制执行步数与时间（默认 5 秒 / 100 万次回调）。
- 仅标准库 `sqlite3`，无第三方依赖；不发起任何外部连接。

## 组件协作

```
用户请求（看结构 / 只读查询 / 索引评估）
   │
   ▼
skills/sqlite-inspection ── 何时查、怎么解读执行计划、如何验收
   │
   ▼
tools/sqlite_inspector_tool.py ── SqliteInspectorTool
   ├── list_tables ──► 表清单（可选行数）
   ├── describe_table ──► 列 / 索引 / 外键 / 建表语句
   └── query ──► 限量只读 SELECT + EXPLAIN QUERY PLAN
   （mode=ro + query_only + authorizer + 资源上限）
```

本插件不包含 rail。

## 目录结构

```
sqlite-inspector/
├── manifest.json
├── README.md
├── LICENSE                    # Apache-2.0（与仓库根一致）
├── PROVENANCE.md
├── skills/sqlite-inspection/SKILL.md
└── tools/sqlite_inspector_tool.py
```
