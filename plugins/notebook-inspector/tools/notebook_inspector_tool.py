# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Notebook 检查工具：本地 .ipynb 的只读静态检查与清洗导出。

继承 ``openjiuwen.core.foundation.tool.Tool``。纯 JSON 解析，绝不执行任何 cell；
预览自动跳过图片 / base64 内容并做长度截断；
export 仅写入显式传入的全新 output_path（独占创建，绝不覆盖源文件或任何已存在文件）。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard

_TEXT_MIME_KEYS = ("text/plain", "text/markdown", "text/latex", "text/html")


class NotebookInspectorTool(Tool):
    """静态检查本地 .ipynb：cell 大纲 / 有界预览 / 历史错误 / 执行顺序提示；可导出清洗副本。"""

    MAX_FILE_BYTES = 20 * 1024 * 1024
    DEFAULT_MAX_CELLS = 100
    MAX_CELLS_LIMIT = 500
    DEFAULT_PREVIEW_CHARS = 200
    PREVIEW_CHARS_LIMIT = 2000
    MAX_ERRORS_PER_CELL = 5
    MAX_ORDER_ISSUES = 10
    MAX_MALFORMED_REPORTED = 20

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="notebook_inspector",
                name="notebook_inspector",
                description=(
                    "Notebook 静态检查工具：只读解析本地 .ipynb 的 JSON，绝不执行任何 cell。"
                    "action=inspect 返回 cell 大纲、源码与文本输出的有界预览"
                    "（图片 / base64 自动跳过）、记录到的错误以及 execution_count 顺序提示；"
                    "action=export_clean 移除 code cell 的 outputs / execution_count 后"
                    "导出清洗副本到全新的 output_path（拒绝覆盖源文件或任何已存在文件）。"
                    "需要在不运行 notebook 的情况下了解其内容、定位历史报错或分发干净版本时调用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["inspect", "export_clean"],
                            "description": "操作：inspect=只读检查；export_clean=导出去除 outputs/execution_count 的清洗副本",
                        },
                        "notebook_path": {
                            "type": "string",
                            "description": "本地 .ipynb 文件路径（只读，绝不执行、绝不修改）",
                        },
                        "output_path": {
                            "type": "string",
                            "description": "export_clean 必填：全新的输出文件路径；不得已存在，不得等于源文件",
                        },
                        "max_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "description": "inspect 最多列出的 cell 数量（默认 100）",
                        },
                        "preview_chars": {
                            "type": "integer",
                            "minimum": 16,
                            "maximum": 2000,
                            "description": "每个 cell 源码 / 输出预览的最大字符数（默认 200）",
                        },
                    },
                    "required": ["action", "notebook_path"],
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        params = self._extract_params(inputs)
        action = params.get("action")
        if action not in ("inspect", "export_clean"):
            return {
                "success": False,
                "error": "参数 action 必须为 'inspect' 或 'export_clean'",
            }
        notebook_path = params.get("notebook_path")
        if not isinstance(notebook_path, str) or not notebook_path.strip():
            return {
                "success": False,
                "error": "缺少必填参数 notebook_path：待检查的本地 .ipynb 路径",
            }
        notebook, error = self._load_notebook(notebook_path)
        if error is not None:
            return {"success": False, "error": error}
        if action == "inspect":
            return self._inspect(notebook, notebook_path, params)
        return self._export_clean(notebook, notebook_path, params)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _extract_params(inputs: Any) -> dict[str, Any]:
        keys = ("action", "notebook_path", "output_path", "max_cells", "preview_chars")
        if isinstance(inputs, dict):
            return {k: inputs[k] for k in keys if k in inputs}
        result: dict[str, Any] = {}
        for k in keys:
            v = getattr(inputs, k, None)
            if v is not None:
                result[k] = v
        return result

    def _load_notebook(self, notebook_path: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            real = os.path.realpath(notebook_path)
        except OSError as exc:
            return None, f"路径不可用：{exc}"
        if not os.path.isfile(real):
            return None, f"文件不存在或不是普通文件：{notebook_path}"
        try:
            size = os.path.getsize(real)
        except OSError as exc:
            return None, f"无法读取文件大小：{exc}"
        if size > self.MAX_FILE_BYTES:
            return None, (
                f"文件过大（{size} 字节，上限 {self.MAX_FILE_BYTES} 字节）：{notebook_path}"
            )
        try:
            with open(real, "r", encoding="utf-8-sig") as fh:
                notebook = json.load(fh)
        except UnicodeDecodeError:
            return None, "文件不是 UTF-8 文本，无法按 notebook JSON 解析"
        except json.JSONDecodeError as exc:
            return None, f"不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"
        except RecursionError:
            return None, "JSON 嵌套层级过深，拒绝解析"
        except OSError as exc:
            return None, f"读取文件失败：{exc}"
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
            return None, "不是有效的 notebook 结构：顶层必须是含 'cells' 列表的 JSON 对象"
        return notebook, None

    def _bounded_int(self, value: Any, default: int, limit: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        if value < 1:
            return 1
        return min(value, limit)

    def _inspect(
        self,
        notebook: dict[str, Any],
        notebook_path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        max_cells = self._bounded_int(
            params.get("max_cells"), self.DEFAULT_MAX_CELLS, self.MAX_CELLS_LIMIT
        )
        preview_chars = self._bounded_int(
            params.get("preview_chars"),
            self.DEFAULT_PREVIEW_CHARS,
            self.PREVIEW_CHARS_LIMIT,
        )
        cells = notebook["cells"]
        total = len(cells)
        listed = min(total, max_cells)
        summaries = [self._summarize_cell(cells[i], i, preview_chars) for i in range(listed)]
        malformed = [
            i
            for i in range(total)
            if not isinstance(cells[i], dict)
            or not isinstance(cells[i].get("cell_type"), str)
        ]
        result: dict[str, Any] = {
            "success": True,
            "action": "inspect",
            "notebook_path": notebook_path,
            "nbformat": self._nbformat_info(notebook),
            "kernel": self._kernel_name(notebook),
            "total_cells": total,
            "cells_listed": listed,
            "cells_truncated": total - listed,
            "cells": summaries,
            "malformed_cells": malformed[: self.MAX_MALFORMED_REPORTED],
            "malformed_cells_total": len(malformed),
        }
        result.update(self._execution_order_hints(cells))
        return result

    def _summarize_cell(self, cell: Any, index: int, preview_chars: int) -> dict[str, Any]:
        if not isinstance(cell, dict):
            return {"index": index, "type": "invalid", "error": "cell 不是 JSON 对象"}
        cell_type = cell.get("cell_type")
        if not isinstance(cell_type, str) or not cell_type:
            entry: dict[str, Any] = {"index": index, "type": "unknown", "error": "缺少 cell_type"}
        else:
            entry = {"index": index, "type": cell_type}
        source = self._join_source(cell.get("source"))
        entry["source_lines"] = len(source.splitlines())
        entry["source_chars"] = len(source)
        entry["source_preview"], entry["preview_truncated"] = self._clip(source, preview_chars)
        if cell_type == "code":
            ec = cell.get("execution_count")
            entry["execution_count"] = ec if isinstance(ec, int) else None
            outputs = cell.get("outputs")
            outputs = outputs if isinstance(outputs, list) else []
            entry["has_outputs"] = len(outputs) > 0
            entry["output_count"] = len(outputs)
            entry.update(self._summarize_outputs(outputs, preview_chars))
        return entry

    def _summarize_outputs(self, outputs: list[Any], preview_chars: int) -> dict[str, Any]:
        kinds: list[str] = []
        skipped_mimes: list[str] = []
        errors: list[dict[str, str]] = []
        errors_total = 0
        chunks: list[str] = []
        for out in outputs:
            if not isinstance(out, dict):
                kinds.append("invalid")
                continue
            otype = out.get("output_type")
            otype = otype if isinstance(otype, str) else "unknown"
            chunk = ""
            if otype == "stream":
                kinds.append("stream")
                chunk = self._join_source(out.get("text"))
            elif otype == "error":
                kinds.append("error")
                errors_total += 1
                ename = str(out.get("ename", "?"))[:80]
                evalue = str(out.get("evalue", ""))[:200]
                if len(errors) < self.MAX_ERRORS_PER_CELL:
                    errors.append({"ename": ename, "evalue": evalue})
                chunk = f"{ename}: {evalue}"
            elif otype in ("display_data", "execute_result"):
                data = out.get("data")
                data = data if isinstance(data, dict) else {}
                mimes = [m for m in data.keys() if isinstance(m, str)]
                if mimes:
                    kinds.append(f"{otype}[{','.join(mimes[:6])}]")
                else:
                    kinds.append(otype)
                skipped_mimes.extend(m for m in mimes if m not in _TEXT_MIME_KEYS)
                for mime in _TEXT_MIME_KEYS:
                    if mime in data:
                        chunk = self._join_source(data.get(mime))
                        break
            else:
                kinds.append(otype)
            if chunk:
                chunks.append(chunk)
        preview, truncated = self._clip("\n".join(chunks), preview_chars)
        return {
            "output_kinds": kinds[:10],
            "skipped_mimes": sorted(set(skipped_mimes))[:8],
            "errors": errors,
            "errors_total": errors_total,
            "has_errors": errors_total > 0,
            "output_preview": preview,
            "output_preview_truncated": truncated,
        }

    def _execution_order_hints(self, cells: list[Any]) -> dict[str, Any]:
        counts: list[tuple[int, int]] = []
        unexecuted_with_outputs = 0
        for i, cell in enumerate(cells):
            if not isinstance(cell, dict) or cell.get("cell_type") != "code":
                continue
            ec = cell.get("execution_count")
            if isinstance(ec, int):
                counts.append((i, ec))
            else:
                outputs = cell.get("outputs")
                if isinstance(outputs, list) and outputs:
                    unexecuted_with_outputs += 1
        issues: list[str] = []
        seen_max: int | None = None
        by_count: dict[int, list[int]] = {}
        for idx, ec in counts:
            by_count.setdefault(ec, []).append(idx)
            if seen_max is not None and ec < seen_max:
                issues.append(
                    f"cell[{idx}] execution_count={ec} 小于此前最大值 {seen_max}，"
                    "文档顺序与执行顺序可能不一致"
                )
            seen_max = ec if seen_max is None else max(seen_max, ec)
        for ec in sorted(by_count):
            idxs = by_count[ec]
            if len(idxs) > 1:
                issues.append(
                    f"execution_count={ec} 被 {len(idxs)} 个 cell 重复使用（cell{idxs[:5]}）"
                )
        return {
            "execution_order": {
                "executed_cells": len(counts),
                "max_execution_count": max((ec for _, ec in counts), default=None),
                "unexecuted_with_outputs": unexecuted_with_outputs,
                "in_document_order": not issues,
                "issues": issues[: self.MAX_ORDER_ISSUES],
                "issues_truncated": max(0, len(issues) - self.MAX_ORDER_ISSUES),
                "note": (
                    "提示仅为启发式：execution_count 反映内核执行历史，"
                    "可能因重跑或乱序执行与文档顺序不同"
                ),
            }
        }

    def _export_clean(
        self,
        notebook: dict[str, Any],
        notebook_path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        output_path = params.get("output_path")
        if not isinstance(output_path, str) or not output_path.strip():
            return {
                "success": False,
                "error": "export_clean 需要显式的 output_path（全新的输出文件路径）",
            }
        src_real = os.path.normcase(os.path.realpath(notebook_path))
        dst_real = os.path.realpath(output_path)
        if src_real == os.path.normcase(dst_real):
            return {
                "success": False,
                "error": "output_path 不得等于源 notebook 路径（绝不修改源文件）",
            }
        if os.path.lexists(output_path) or os.path.lexists(dst_real):
            return {
                "success": False,
                "error": f"output_path 已存在，拒绝覆盖：{output_path}",
            }
        parent = os.path.dirname(dst_real)
        if not os.path.isdir(parent):
            return {
                "success": False,
                "error": f"输出目录不存在：{parent}",
            }
        cleaned = copy.deepcopy(notebook)
        cleaned_count = 0
        for cell in cleaned.get("cells", []):
            if not isinstance(cell, dict) or cell.get("cell_type") != "code":
                continue
            cell.pop("outputs", None)
            cell.pop("execution_count", None)
            cleaned_count += 1
        try:
            with open(dst_real, "x", encoding="utf-8") as fh:
                json.dump(cleaned, fh, ensure_ascii=False, indent=1)
                fh.write("\n")
        except FileExistsError:
            return {
                "success": False,
                "error": f"output_path 已存在，拒绝覆盖：{output_path}",
            }
        except OSError as exc:
            return {"success": False, "error": f"写入输出文件失败：{exc}"}
        return {
            "success": True,
            "action": "export_clean",
            "notebook_path": notebook_path,
            "output_path": output_path,
            "total_cells": len(cleaned.get("cells", [])),
            "code_cells_cleaned": cleaned_count,
            "removed": ["outputs", "execution_count"],
        }

    @staticmethod
    def _nbformat_info(notebook: dict[str, Any]) -> dict[str, Any]:
        major = notebook.get("nbformat")
        minor = notebook.get("nbformat_minor")
        info: dict[str, Any] = {
            "major": major if isinstance(major, int) else None,
            "minor": minor if isinstance(minor, int) else None,
        }
        if info["major"] is None:
            info["warning"] = "缺少 nbformat 字段，可能是非标准 notebook"
        return info

    @staticmethod
    def _kernel_name(notebook: dict[str, Any]) -> str | None:
        meta = notebook.get("metadata")
        if not isinstance(meta, dict):
            return None
        ks = meta.get("kernelspec")
        if isinstance(ks, dict) and isinstance(ks.get("name"), str):
            return ks["name"]
        return None

    @staticmethod
    def _join_source(source: Any) -> str:
        if isinstance(source, str):
            return source
        if isinstance(source, list):
            return "".join(part for part in source if isinstance(part, str))
        return ""

    @staticmethod
    def _clip(text: str, limit: int) -> tuple[str, bool]:
        if limit <= 0:
            return "", bool(text)
        if len(text) <= limit:
            return text, False
        return text[:limit], True
