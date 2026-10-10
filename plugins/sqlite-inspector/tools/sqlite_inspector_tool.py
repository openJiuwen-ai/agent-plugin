# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SQLite 检查工具：本地数据库文件的只读检查。

继承 ``openjiuwen.core.foundation.tool.Tool``，仅使用标准库 sqlite3。
四重只读防护：URI mode=ro、PRAGMA query_only、sqlite3 authorizer
（拒绝写入 / DDL / ATTACH / PRAGMA 改写 / load_extension）、进度处理器
（执行步数与时间上限）。行数 / 字符数 / 单元格长度均有界。
绝不创建不存在的数据库，绝不写入，不发起任何外部连接。
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard

_ALLOWED_PREFIXES = ("select", "with", "values", "explain")
_ALLOWED_INTROSPECTION_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "table_list",
    }
)
_DENIED_ACTION_NAMES = (
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DELETE",
    "SQLITE_ALTER_TABLE",
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
    "SQLITE_TRANSACTION",
    "SQLITE_SAVEPOINT",
)
_DENIED_ACTIONS = frozenset(
    v
    for v in (getattr(sqlite3, n, None) for n in _DENIED_ACTION_NAMES)
    if v is not None
)


class SqliteInspectorTool(Tool):
    """只读检查本地 SQLite：list_tables / describe_table / query（限量 SELECT + 执行计划）。"""

    MAX_FILE_BYTES = 256 * 1024 * 1024
    DEFAULT_MAX_ROWS = 50
    MAX_ROWS_LIMIT = 1000
    MAX_CELL_CHARS = 4096
    MAX_TOTAL_CHARS = 65536
    MAX_TABLES_LISTED = 500
    MAX_PARAMS = 32
    MAX_SQL_CHARS = 10000
    MAX_EXPLAIN_ROWS = 50
    QUERY_TIME_CAP_SECONDS = 5.0
    QUERY_OPS_CAP = 1_000_000
    PROGRESS_INSTRUCTIONS = 500
    BUSY_TIMEOUT_SECONDS = 5.0

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="sqlite_inspector",
                name="sqlite_inspector",
                description=(
                    "SQLite 只读检查工具：检查显式指定的本地数据库文件。"
                    "action=list_tables 列出表与视图（可选行数）；"
                    "action=describe_table 查看列 / 索引 / 外键；"
                    "action=query 执行带参数的单条只读 SELECT/WITH/VALUES 并附 EXPLAIN QUERY PLAN。"
                    "mode=ro + query_only + authorizer + 资源上限四重防护，绝不写入、绝不建库、"
                    "拒绝多语句 / 写语句 / ATTACH / PRAGMA 改写 / 扩展加载。"
                    "需要安全地查看数据库结构或做只读排查时调用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list_tables", "describe_table", "query"],
                            "description": (
                                "操作：list_tables=列出表与视图；describe_table=列/索引/外键；"
                                "query=限量只读查询（附执行计划）"
                            ),
                        },
                        "db_path": {
                            "type": "string",
                            "description": "本地 SQLite 数据库文件路径（必须已存在；只读打开，绝不创建）",
                        },
                        "table": {
                            "type": "string",
                            "description": "describe_table 必填：表名",
                        },
                        "sql": {
                            "type": "string",
                            "description": "query 必填：单条 SELECT / WITH / VALUES 语句",
                        },
                        "params": {
                            "description": "SQL 绑定参数：位置参数数组或命名参数对象",
                        },
                        "max_rows": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "description": "query 最多返回行数（默认 50）",
                        },
                        "include_row_counts": {
                            "type": "boolean",
                            "description": "list_tables 时是否统计每表行数（大表可能较慢）",
                        },
                    },
                    "required": ["action", "db_path"],
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        params = self._extract_params(inputs)
        action = params.get("action")
        if action not in ("list_tables", "describe_table", "query"):
            return {
                "success": False,
                "error": "参数 action 必须为 'list_tables' / 'describe_table' / 'query'",
            }
        db_path = params.get("db_path")
        if not isinstance(db_path, str) or not db_path.strip():
            return {
                "success": False,
                "error": "缺少必填参数 db_path：已存在的本地 SQLite 数据库文件路径",
            }
        conn, guard, error = self._open_readonly(db_path)
        if error is not None:
            return {"success": False, "error": error}
        try:
            if action == "list_tables":
                return self._list_tables(conn, db_path, params)
            if action == "describe_table":
                return self._describe_table(conn, db_path, params)
            return self._query(conn, guard, db_path, params)
        except sqlite3.Error as exc:
            reason = guard.get("aborted") or str(exc)
            return {"success": False, "error": f"数据库操作被拒绝或失败：{reason}"}
        finally:
            conn.close()

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _extract_params(inputs: Any) -> dict[str, Any]:
        keys = (
            "action",
            "db_path",
            "table",
            "sql",
            "params",
            "max_rows",
            "include_row_counts",
        )
        if isinstance(inputs, dict):
            return {k: inputs[k] for k in keys if k in inputs}
        result: dict[str, Any] = {}
        for k in keys:
            v = getattr(inputs, k, None)
            if v is not None:
                result[k] = v
        return result

    @staticmethod
    def _authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, trigger: Any) -> int:
        if action == getattr(sqlite3, "SQLITE_FUNCTION", -1):
            if isinstance(arg1, str) and arg1.lower() == "load_extension":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == getattr(sqlite3, "SQLITE_PRAGMA", -2):
            if isinstance(arg1, str) and arg1.lower() in _ALLOWED_INTROSPECTION_PRAGMAS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in _DENIED_ACTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _open_readonly(
        self, db_path: str
    ) -> tuple[sqlite3.Connection | None, dict[str, Any], str | None]:
        try:
            real = os.path.realpath(db_path)
        except OSError as exc:
            return None, {}, f"路径不可用：{exc}"
        if not os.path.isfile(real):
            return None, {}, f"数据库文件不存在（本工具绝不创建新数据库）：{db_path}"
        try:
            size = os.path.getsize(real)
        except OSError as exc:
            return None, {}, f"无法读取文件大小：{exc}"
        if size > self.MAX_FILE_BYTES:
            return None, {}, (
                f"数据库文件过大（{size} 字节，上限 {self.MAX_FILE_BYTES} 字节）：{db_path}"
            )
        try:
            uri = Path(real).as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=self.BUSY_TIMEOUT_SECONDS)
            conn.execute("PRAGMA query_only = ON")
            try:
                conn.enable_load_extension(False)
            except (AttributeError, sqlite3.Error):
                pass
            conn.set_authorizer(self._authorizer)
            deadline = time.monotonic() + self.QUERY_TIME_CAP_SECONDS
            guard: dict[str, Any] = {"ops": 0, "aborted": None}

            def _progress() -> int:
                guard["ops"] = guard["ops"] + 1
                if guard["ops"] > self.QUERY_OPS_CAP:
                    guard["aborted"] = (
                        f"查询执行超限（>{self.QUERY_OPS_CAP} 次进度回调），已中止"
                    )
                    return 1
                if time.monotonic() > deadline:
                    guard["aborted"] = (
                        f"查询执行超时（上限 {self.QUERY_TIME_CAP_SECONDS:.0f} 秒），已中止"
                    )
                    return 1
                return 0

            conn.set_progress_handler(_progress, self.PROGRESS_INSTRUCTIONS)
        except (sqlite3.Error, ValueError, OSError) as exc:
            return None, {}, f"打开数据库失败：{exc}"
        return conn, guard, None

    @staticmethod
    def _quote_ident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    @staticmethod
    def _quote_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _first_keyword(sql: str) -> str:
        stripped = sql.lstrip()
        return stripped.split(None, 1)[0].lower() if stripped else ""

    @staticmethod
    def _is_single_statement(sql: str) -> bool:
        stripped = sql.strip()
        if not stripped:
            return False
        count = 0
        buf = ""
        for ch in stripped:
            buf += ch
            if ch == ";" and sqlite3.complete_statement(buf):
                count += 1
                buf = ""
        if count > 1:
            return False
        if count == 1 and buf.strip():
            return False
        return True

    def _normalize_params(self, raw: Any) -> tuple[list | dict | None, str | None]:
        if raw is None:
            return None, None
        if isinstance(raw, list):
            if len(raw) > self.MAX_PARAMS:
                return None, f"params 数量超限（>{self.MAX_PARAMS}）"
            for p in raw:
                if not isinstance(p, (str, int, float, type(None))):
                    return None, "params 仅支持 str / int / float / null 元素"
            return raw, None
        if isinstance(raw, dict):
            if len(raw) > self.MAX_PARAMS:
                return None, f"params 数量超限（>{self.MAX_PARAMS}）"
            for k, v in raw.items():
                if not isinstance(k, str) or not k.isidentifier():
                    return None, "命名参数的键必须是合法标识符"
                if not isinstance(v, (str, int, float, type(None))):
                    return None, "params 仅支持 str / int / float / null 值"
            return raw, None
        return None, "params 必须是数组（位置参数）或对象（命名参数）"

    @classmethod
    def _render_cell(cls, value: Any) -> tuple[Any, int]:
        if value is None or isinstance(value, (bool, int, float)):
            return value, 8
        if isinstance(value, str):
            if len(value) > cls.MAX_CELL_CHARS:
                marker = "…[截断]"
                clipped = value[: cls.MAX_CELL_CHARS - len(marker)] + marker
                return clipped, len(value)
            return value, len(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<BLOB {len(value)} 字节，内容不返回>", 16
        text = str(value)
        return text[: cls.MAX_CELL_CHARS], len(text)

    def _bounded_int(self, value: Any, default: int, limit: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        if value < 1:
            return 1
        return min(value, limit)

    def _list_tables(
        self, conn: sqlite3.Connection, db_path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        include_counts = params.get("include_row_counts") is True
        cur = conn.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name LIMIT ?",
            (self.MAX_TABLES_LISTED + 1,),
        )
        raw = cur.fetchall()
        truncated = len(raw) > self.MAX_TABLES_LISTED
        raw = raw[: self.MAX_TABLES_LISTED]
        tables: list[dict[str, Any]] = []
        for entry_type, name in raw:
            item: dict[str, Any] = {"name": name, "type": entry_type}
            if include_counts and entry_type == "table":
                try:
                    cnt = conn.execute(
                        f"SELECT COUNT(*) FROM {self._quote_ident(name)}"
                    ).fetchone()
                    item["row_count"] = cnt[0] if cnt else None
                except sqlite3.Error:
                    item["row_count"] = None
                    item["row_count_error"] = "统计失败（可能被资源上限中止）"
            tables.append(item)
        result: dict[str, Any] = {
            "success": True,
            "action": "list_tables",
            "db_path": db_path,
            "tables": tables,
            "total_listed": len(tables),
            "truncated": truncated,
            "note": "已过滤 sqlite_ 前缀内部表；row_count 为全表扫描计数，受资源上限约束",
        }
        if not include_counts:
            result["note"] += "；如需行数请传 include_row_counts=true"
        return result

    def _describe_table(
        self, conn: sqlite3.Connection, db_path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        table = params.get("table")
        if not isinstance(table, str) or not table.strip():
            return {"success": False, "error": "describe_table 需要参数 table：表名"}
        table = table.strip()
        row = conn.execute(
            "SELECT type, sql FROM sqlite_schema WHERE name = ? LIMIT 1", (table,)
        ).fetchone()
        if row is None:
            return {"success": False, "error": f"表或视图不存在：{table}"}
        entry_type, create_sql = row
        lit = self._quote_literal(table)
        columns = []
        for cid, name, col_type, notnull, dflt, pk in conn.execute(
            f"SELECT * FROM pragma_table_info({lit})"
        ).fetchall():
            columns.append(
                {
                    "cid": cid,
                    "name": name,
                    "type": col_type,
                    "notnull": bool(notnull),
                    "default_value": dflt,
                    "primary_key": pk,
                }
            )
        indexes = []
        for _seq, iname, unique, origin, partial in conn.execute(
            f"SELECT * FROM pragma_index_list({lit})"
        ).fetchall()[:20]:
            idx_columns = [
                c[2] for c in conn.execute(
                    f"SELECT * FROM pragma_index_info({self._quote_literal(iname)})"
                ).fetchall()
            ]
            indexes.append(
                {
                    "name": iname,
                    "unique": bool(unique),
                    "origin": origin,
                    "partial": bool(partial),
                    "columns": idx_columns,
                }
            )
        foreign_keys = []
        for fk_row in conn.execute(
            f"SELECT * FROM pragma_foreign_key_list({lit})"
        ).fetchall()[:20]:
            foreign_keys.append(
                {
                    "column": fk_row[3],
                    "references_table": fk_row[2],
                    "references_column": fk_row[4],
                    "on_update": fk_row[5],
                    "on_delete": fk_row[6],
                }
            )
        create_sql_text = create_sql or ""
        create_sql_clipped = create_sql_text[:2000]
        return {
            "success": True,
            "action": "describe_table",
            "db_path": db_path,
            "table": table,
            "type": entry_type,
            "columns": columns,
            "indexes": indexes,
            "foreign_keys": foreign_keys,
            "create_sql": create_sql_clipped,
            "create_sql_truncated": len(create_sql_text) > 2000,
        }

    def _query(
        self,
        conn: sqlite3.Connection,
        guard: dict[str, Any],
        db_path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        sql = params.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return {"success": False, "error": "query 需要参数 sql：单条只读查询语句"}
        if len(sql) > self.MAX_SQL_CHARS:
            return {"success": False, "error": f"sql 过长（上限 {self.MAX_SQL_CHARS} 字符）"}
        keyword = self._first_keyword(sql)
        if keyword not in _ALLOWED_PREFIXES:
            return {
                "success": False,
                "error": (
                    f"仅允许 SELECT / WITH / VALUES / EXPLAIN 只读语句（检测到开头：{keyword or '(空)'}）；"
                    "写入、PRAGMA、ATTACH 等均被拒绝"
                ),
            }
        if not self._is_single_statement(sql):
            return {"success": False, "error": "拒绝多语句 SQL：仅允许单条语句"}
        bind, bind_error = self._normalize_params(params.get("params"))
        if bind_error is not None:
            return {"success": False, "error": bind_error}
        bind_args: list | dict = bind if bind is not None else []
        started = time.monotonic()

        explain_rows: list[dict[str, Any]] = []
        if keyword != "explain":
            try:
                cur = conn.execute("EXPLAIN QUERY PLAN " + sql, bind_args)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall()[: self.MAX_EXPLAIN_ROWS]:
                    explain_rows.append(
                        {"detail": str(row[-1])[:500], "raw_columns": cols}
                    )
            except sqlite3.Error as exc:
                return {"success": False, "error": f"查询校验失败：{exc}"}

        max_rows = self._bounded_int(
            params.get("max_rows"), self.DEFAULT_MAX_ROWS, self.MAX_ROWS_LIMIT
        )
        cur = conn.execute(sql, bind_args)
        column_names = [d[0] for d in cur.description] if cur.description else []
        rows: list[list[Any]] = []
        total_chars = 0
        truncated_rows = False
        truncated_bytes = False
        while True:
            batch = cur.fetchmany(64)
            if not batch:
                break
            for raw in batch:
                if len(rows) >= max_rows + 1:
                    truncated_rows = True
                    break
                rendered: list[Any] = []
                row_chars = 0
                for value in raw:
                    cell, chars = self._render_cell(value)
                    rendered.append(cell)
                    row_chars += chars
                if total_chars + row_chars > self.MAX_TOTAL_CHARS:
                    truncated_bytes = True
                    break
                rows.append(rendered)
                total_chars += row_chars
            if truncated_rows or truncated_bytes:
                break
        if truncated_rows and len(rows) > max_rows:
            rows = rows[:max_rows]
        return {
            "success": True,
            "action": "query",
            "db_path": db_path,
            "sql": sql,
            "columns": column_names,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated_rows or truncated_bytes,
            "truncation_reason": (
                "行数达到 max_rows 上限" if truncated_rows and not truncated_bytes
                else "结果字符数达到上限" if truncated_bytes and not truncated_rows
                else "行数与字符数均达到上限" if truncated_rows
                else None
            ),
            "result_chars": total_chars,
            "explain_query_plan": explain_rows,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "read_only_guarantees": [
                "URI mode=ro 只读打开",
                "PRAGMA query_only",
                "authorizer 拒绝写入/ATTACH/PRAGMA 改写/扩展加载",
                "进度处理器限制步数与时间",
            ],
        }
