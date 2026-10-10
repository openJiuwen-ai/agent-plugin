# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Git 历史追踪工具：本地仓库的只读文件历史 / 行级 blame / 提交证据。

继承 ``openjiuwen.core.foundation.tool.Tool``。仅执行只读 git 子命令
（log / blame / show / rev-parse / check-attr / diff-tree），无 shell、
列表传参、路径必须是仓库内相对路径（拒绝穿越与选项注入）、引用一律先经
``rev-parse --verify --end-of-options`` 解析为完整 SHA 再使用。
``show`` 强制 ``--no-ext-diff --no-textconv``；``blame`` 前置
``check-attr`` 检查自定义 diff 驱动，命中即拒绝。全程
``--no-optional-locks --no-pager``，不改动工作区 / 索引；
输出条数与字符数有界；rename / merge 的证据局限在结果中显式说明。
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard

_BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NOT_COMMITTED_SHA = "0" * 40
GIT_TIMEOUT_SECONDS = 15.0
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 200
MAX_BLAME_SPAN = 500
DEFAULT_BLAME_END = 50
MAX_PATCH_CHARS = 8000
MAX_CONTENT_CHARS = 200


def _run_git(args, cwd, timeout):
    """执行只读 git 命令（无 shell、列表参数）。

    返回 (returncode, stdout, stderr)；git CLI 缺失返回 None；
    超时抛出 subprocess.TimeoutExpired。stdout / stderr 以 UTF-8 容错解码。
    """
    exe = shutil.which("git")
    if exe is None:
        return None
    proc = subprocess.run(
        [exe, "--no-optional-locks", "--no-pager", "-C", str(cwd)]
        + [str(a) for a in args],
        capture_output=True,
        timeout=timeout,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _tz_to_offset(tz_text: str) -> timezone:
    try:
        sign = -1 if str(tz_text).startswith("-") else 1
        hours = int(str(tz_text)[1:3])
        minutes = int(str(tz_text)[3:5])
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    except (ValueError, IndexError, TypeError):
        return timezone.utc


class GitHistoryTraceTool(Tool):
    """只读追踪本地 Git 文件历史：history / blame / show_commit。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="git_history_trace",
                name="git_history_trace",
                description=(
                    "Git 历史追踪工具（只读）：追踪仓库内某文件的提交历史"
                    "（SHA/作者/日期/变更类型，可选跟随重命名）、"
                    "按行区间查看 blame 归属（每行的 SHA、作者、时间、提交摘要）、"
                    "展示某个影响该文件的提交（元数据 + 可选有界 diff）。"
                    "引用先经 rev-parse --verify --end-of-options 解析为 SHA；"
                    "路径拒绝穿越与选项注入；show 强制 --no-ext-diff --no-textconv；"
                    "blame 前置自定义 diff 驱动检查；"
                    "绝不 checkout/reset/commit/fetch，不改动工作区。"
                    "需要回答『这段代码是谁何时为什么改的』时调用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["history", "blame", "show_commit"],
                            "description": (
                                "操作：history=文件提交历史；blame=行区间归属；"
                                "show_commit=单个提交的证据"
                            ),
                        },
                        "repo_path": {
                            "type": "string",
                            "description": "本地 Git 仓库路径（只读访问，绝不改动工作区）",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "仓库内相对文件路径（拒绝绝对路径与 .. 穿越）",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "description": "history 最多返回的提交数（默认 20）",
                        },
                        "follow_renames": {
                            "type": "boolean",
                            "description": "history 是否跟随重命名（--follow，默认 false）",
                        },
                        "start_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "blame 起始行号（1 起，默认 1）",
                        },
                        "end_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "blame 结束行号（含端点，默认 50；区间上限 500 行）",
                        },
                        "commit": {
                            "type": "string",
                            "description": (
                                "show_commit 必填：提交引用（SHA / 分支 / 标签 / HEAD~n），"
                                "先验证再解析为完整 SHA"
                            ),
                        },
                        "include_patch": {
                            "type": "boolean",
                            "description": "show_commit 是否附带该文件的有界 diff（默认 false）",
                        },
                    },
                    "required": ["action", "repo_path", "file_path"],
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        params = self._extract_params(inputs)
        action = params.get("action")
        if action not in ("history", "blame", "show_commit"):
            return {
                "success": False,
                "error": "参数 action 必须为 'history' / 'blame' / 'show_commit'",
            }
        repo_path = params.get("repo_path")
        if not isinstance(repo_path, str) or not repo_path.strip():
            return {"success": False, "error": "缺少必填参数 repo_path：本地 Git 仓库路径"}
        file_path, path_error = self._normalize_relpath(params.get("file_path"))
        if path_error is not None:
            return {"success": False, "error": path_error}
        try:
            if action == "history":
                return self._history(repo_path, file_path, params)
            if action == "blame":
                return self._blame(repo_path, file_path, params)
            return self._show_commit(repo_path, file_path, params)
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"git 命令超时（上限 {GIT_TIMEOUT_SECONDS:.0f} 秒）已中止",
            }
        except OSError as exc:
            return {"success": False, "error": f"无法执行 git CLI：{exc}"}

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _extract_params(inputs: Any) -> dict[str, Any]:
        keys = (
            "action",
            "repo_path",
            "file_path",
            "limit",
            "follow_renames",
            "start_line",
            "end_line",
            "commit",
            "include_patch",
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
    def _normalize_relpath(raw: Any) -> tuple[str | None, str | None]:
        if not isinstance(raw, str) or not raw.strip():
            return None, "缺少必填参数 file_path：仓库内相对路径"
        text = raw.strip().replace("\\", "/")
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            return None, f"拒绝绝对路径：file_path 必须是仓库内相对路径（当前：{raw}）"
        if text.startswith("~"):
            return None, "拒绝以 ~ 开头的路径"
        norm = posixpath.normpath(text)
        if norm in (".", "..", "") or norm.startswith("../"):
            return None, f"拒绝包含 '..' 的路径穿越：{raw}"
        if norm.startswith("-"):
            return None, f"拒绝以 '-' 开头的路径（防选项注入）：{raw}"
        if any(ord(ch) < 32 for ch in norm):
            return None, "路径包含控制字符，已拒绝"
        return norm, None

    def _run(self, args: list[str], cwd: str) -> tuple[str | None, dict[str, Any] | None]:
        """执行并归一化；失败返回 (None, error_dict)。"""
        result = _run_git(args, cwd, GIT_TIMEOUT_SECONDS)
        if result is None:
            return None, {
                "success": False,
                "error": "未找到 git CLI：请确认 git 已安装并在 PATH 中",
            }
        rc, stdout, stderr = result
        if rc != 0:
            return None, {
                "success": False,
                "error": f"git {args[0]} 失败（exit={rc}）",
                "error_detail": self._clip(stderr or stdout, 400),
            }
        return stdout, None

    def _repo_root(self, repo_path: str) -> tuple[str | None, str | None]:
        if not os.path.isdir(repo_path):
            return None, f"仓库路径不存在或不是目录：{repo_path}"
        stdout, error = self._run(["rev-parse", "--show-toplevel"], repo_path)
        if error is not None:
            if "git CLI" in error.get("error", ""):
                return None, error["error"]
            return None, "不是有效的 Git 仓库（git rev-parse 失败）"
        root = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
        if not root or not os.path.isdir(root):
            return None, "无法解析仓库根目录"
        return root, None

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = text or ""
        if len(text) <= limit:
            return text
        return text[:limit] + "…[截断]"

    def _history(
        self, repo_path: str, file_path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        root, error = self._repo_root(repo_path)
        if error is not None:
            return {"success": False, "error": error}
        limit_raw = params.get("limit")
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, int):
            limit = DEFAULT_HISTORY_LIMIT
        else:
            limit = max(1, min(limit_raw, MAX_HISTORY_LIMIT))
        follow = params.get("follow_renames") is True
        args = [
            "log",
            "-n",
            str(limit),
            "--name-status",
            "-M",
        ]
        if follow:
            args.append("--follow")
        args += [
            "--format=%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
            "--",
            file_path,
        ]
        stdout, error = self._run(args, root)
        if error is not None:
            return error
        commits = []
        for chunk in stdout.split("\x1e")[1:]:
            chunk = chunk.lstrip("\n")
            if not chunk.strip():
                continue
            lines = chunk.split("\n")
            header = lines[0].split("\x1f")
            if len(header) != 5:
                continue
            changes = []
            for change_line in lines[1:]:
                change_line = change_line.strip()
                if not change_line:
                    continue
                parts = change_line.split("\t")
                status = parts[0]
                if len(parts) > 2 and status[:1] in ("R", "C"):
                    changes.append(
                        {"status": status, "old_path": parts[1], "path": parts[2]}
                    )
                else:
                    changes.append(
                        {
                            "status": status,
                            "path": parts[1] if len(parts) > 1 else change_line,
                            "old_path": None,
                        }
                    )
            commits.append(
                {
                    "sha": header[0],
                    "author": header[1],
                    "email": header[2],
                    "date": header[3],
                    "subject": header[4],
                    "changes": changes,
                }
            )
        notes = [
            "合并提交默认受历史简化（history simplification）影响，未列出不代表未影响该文件",
        ]
        if follow:
            notes.insert(
                0,
                "--follow 仅识别单个路径的精确重命名；拷贝检测与跨合并重命名有限制",
            )
        return {
            "success": True,
            "action": "history",
            "repo_root": root,
            "file_path": file_path,
            "worktree_exists": os.path.isfile(os.path.join(root, file_path)),
            "follow_renames": follow,
            "commits": commits,
            "count": len(commits),
            "limit": limit,
            "possibly_more": len(commits) == limit,
            "notes": notes,
        }

    def _blame(
        self, repo_path: str, file_path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        root, error = self._repo_root(repo_path)
        if error is not None:
            return {"success": False, "error": error}
        start = self._line_param(params.get("start_line"), 1)
        end = self._line_param(params.get("end_line"), DEFAULT_BLAME_END)
        if start is None or end is None:
            return {"success": False, "error": "start_line / end_line 必须是 ≥1 的整数"}
        if start > end:
            return {
                "success": False,
                "error": f"start_line（{start}）不得大于 end_line（{end}）",
            }
        if end - start + 1 > MAX_BLAME_SPAN:
            return {
                "success": False,
                "error": f"blame 行区间超限（上限 {MAX_BLAME_SPAN} 行）",
            }
        attr_stdout, attr_error = self._run(
            ["check-attr", "diff", "--", file_path], root
        )
        if attr_error is not None:
            return attr_error
        attr_value = self._parse_check_attr(attr_stdout)
        if attr_value not in ("unspecified", "unset", ""):
            return {
                "success": False,
                "error": (
                    f"文件配置了自定义 diff 驱动（diff={attr_value}），"
                    "为避免 textconv / 外部差异程序执行，已拒绝 blame"
                ),
            }
        stdout, error = self._run(
            ["blame", "--porcelain", "-L", f"{start},{end}", "--", file_path], root
        )
        if error is not None:
            return error
        lines = self._parse_blame(stdout, start, end)
        return {
            "success": True,
            "action": "blame",
            "repo_root": root,
            "file_path": file_path,
            "start_line": start,
            "end_line": end,
            "lines": lines,
            "line_count": len(lines),
            "notes": [
                "行级归属基于内容追踪，跨重命名 / 合并的作者归属可能不完整",
                "boundary=true 表示到达可追溯边界（如初始提交或浅克隆边界）",
                "uncommitted=true 表示该行是工作区未提交修改（Not Committed Yet）",
            ],
        }

    @staticmethod
    def _line_param(raw: Any, default: int) -> int | None:
        if raw is None:
            return default
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            return None
        return raw

    @staticmethod
    def _parse_blame(text: str, start: int, end: int) -> list[dict[str, Any]]:
        """按内容行解析 porcelain 输出。

        git porcelain 会为同组后续行重复输出无 size 的头（如 ``<sha> 4 4``），
        因此行号必须由内容行推进（而非组 size 展开），元数据按 SHA 缓存回填。
        """
        entries: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        meta_cache: dict[str, dict[str, Any]] = {}
        for raw_line in text.splitlines():
            if not raw_line:
                continue
            header = _BLAME_HEADER_RE.match(raw_line)
            if header is not None:
                current = {
                    "sha": header.group(1),
                    "next_line": int(header.group(3)),
                    "meta": {},
                }
                continue
            if raw_line.startswith("\t"):
                if current is None:
                    continue
                meta = (
                    current["meta"]
                    if current["meta"]
                    else meta_cache.get(current["sha"], {})
                )
                line_no = current["next_line"]
                if start <= line_no <= end:
                    iso_time = None
                    raw_time = meta.get("author_time")
                    if isinstance(raw_time, int):
                        try:
                            iso_time = datetime.fromtimestamp(
                                raw_time,
                                tz=_tz_to_offset(meta.get("author_tz", "+0000")),
                            ).isoformat()
                        except (ValueError, OSError, OverflowError):
                            iso_time = None
                    entries.append(
                        {
                            "line": line_no,
                            "sha": current["sha"],
                            "uncommitted": current["sha"] == _NOT_COMMITTED_SHA,
                            "author": meta.get("author"),
                            "author_email": meta.get("author_mail"),
                            "author_time": iso_time,
                            "summary": meta.get("summary"),
                            "boundary": meta.get("boundary", False),
                            "content": raw_line[1:][:MAX_CONTENT_CHARS].rstrip("\r"),
                        }
                    )
                current["next_line"] += 1
                continue
            if current is None:
                continue
            key, _, value = raw_line.partition(" ")
            if key == "author":
                current["meta"]["author"] = value
            elif key == "author-mail":
                current["meta"]["author_mail"] = value
            elif key == "author-time":
                try:
                    current["meta"]["author_time"] = int(value)
                except ValueError:
                    pass
            elif key == "author-tz":
                current["meta"]["author_tz"] = value
            elif key == "summary":
                current["meta"]["summary"] = value
            elif key == "boundary":
                current["meta"]["boundary"] = True
            if current["meta"]:
                meta_cache[current["sha"]] = dict(current["meta"])
        return entries

    def _show_commit(
        self, repo_path: str, file_path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        root, error = self._repo_root(repo_path)
        if error is not None:
            return {"success": False, "error": error}
        commit = params.get("commit")
        if not isinstance(commit, str) or not commit.strip():
            return {
                "success": False,
                "error": "show_commit 需要参数 commit：提交引用（SHA / 分支 / 标签 / HEAD~n）",
            }
        commit = commit.strip()
        if len(commit) > 200 or any(ord(ch) < 32 for ch in commit):
            return {"success": False, "error": "非法的提交引用（含控制字符或过长）"}
        stdout, error = self._run(
            ["rev-parse", "--verify", "--end-of-options", commit], root
        )
        if error is not None:
            return {
                "success": False,
                "error": f"无法解析提交引用 {commit!r}",
                "error_detail": error.get("error_detail"),
            }
        sha = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
        if not _SHA_RE.match(sha):
            return {"success": False, "error": f"引用 {commit!r} 未解析为完整 SHA"}
        meta_stdout, meta_error = self._run(
            [
                "show",
                "--no-patch",
                "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%P%x1f%s",
                "--no-ext-diff",
                "--no-textconv",
                sha,
            ],
            root,
        )
        if meta_error is not None:
            return meta_error
        fields = meta_stdout.strip().split("\x1f")
        while len(fields) < 6:
            fields.append("")
        commit_sha, author, email, date, parents_text, subject = fields[:6]
        parents = parents_text.split()
        is_merge = len(parents) > 1
        result: dict[str, Any] = {
            "success": True,
            "action": "show_commit",
            "repo_root": root,
            "file_path": file_path,
            "requested_ref": commit,
            "commit": {
                "sha": commit_sha,
                "author": author,
                "email": email,
                "date": date,
                "parents": parents,
                "is_merge": is_merge,
                "subject": subject,
            },
            "touches_file": None,
        }
        notes: list[str] = []
        if is_merge:
            notes.append(
                "合并提交：diff-tree 默认不展开合并差异，touches_file 无法可靠判定，证据以元数据为准"
            )
        else:
            tree_stdout, tree_error = self._run(
                [
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "--root",
                    sha,
                    "--",
                    file_path,
                ],
                root,
            )
            if tree_error is not None:
                return tree_error
            touches = bool(tree_stdout.strip())
            if not touches:
                return {
                    "success": False,
                    "error": f"提交 {sha[:12]} 未修改文件 {file_path}",
                }
            result["touches_file"] = True
        if params.get("include_patch") is True:
            patch_stdout, patch_error = self._run(
                [
                    "show",
                    "--format=",
                    "--no-ext-diff",
                    "--no-textconv",
                    "-M",
                    "--unified=3",
                    sha,
                    "--",
                    file_path,
                ],
                root,
            )
            if patch_error is not None:
                result["patch"] = None
                result["patch_error"] = patch_error.get("error_detail")
            else:
                result["patch"] = patch_stdout[:MAX_PATCH_CHARS]
                result["patch_truncated"] = len(patch_stdout) > MAX_PATCH_CHARS
        result["notes"] = notes
        return result

    @staticmethod
    def _parse_check_attr(stdout: str) -> str:
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            normalized = line.replace("\t", ":")
            if ":" in normalized:
                return normalized.rsplit(":", 1)[-1].strip().strip('"')
            parts = normalized.split()
            return parts[-1] if parts else ""
        return ""
