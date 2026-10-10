---
name: sqlite-inspection
description: 本地 SQLite 数据库的只读检查。当用户想查看库里的表结构、索引、外键，或需要在不写入的前提下跑受限 SELECT（含执行计划）时使用；配合 sqlite_inspector 工具，四重防护确保绝不写入。
---

# SQLite 检查（sqlite-inspection）

## 目标

只读回答三类问题：库里有什么（表 / 视图 / 行数）、表长什么样（列 / 索引 / 外键 / 建表语句）、数据查出来是什么（限量 SELECT + EXPLAIN QUERY PLAN）。绝不写入、绝不建库。

## 工作流

1. `sqlite_inspector` action=list_tables：先拿到表清单（需要行数时传 include_row_counts=true，注意大表计数较慢）。
2. action=describe_table：定位目标表的列、索引、外键与建表语句，评估结构与索引覆盖。
3. action=query：单条 SELECT/WITH/VALUES + 命名或位置参数；结合 explain_query_plan 判断是否全表扫描，提出加索引建议（建议本身标注为推断）。
4. 写入 / 多语句 / PRAGMA 改写 / ATTACH 会被拒绝并返回明确错误——这是设计行为，向用户如实转述，不要尝试绕过。

## 验收标准

- 源数据库文件未被修改（可用文件 mtime / hash 复核）。
- 所有查询只读且结果有界：row_count、truncated、truncation_reason 明确。
- 结论区分「证据」（查询结果）与「推断」（索引 / 性能建议）。
