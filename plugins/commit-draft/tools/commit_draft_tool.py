# SPDX-License-Identifier: Apache-2.0
"""Collect local Git evidence without staging, committing or contacting a remote."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, ToolCard


def git(repo, *args):
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "--no-pager", "-C", str(repo), *args],
        capture_output=True, timeout=30, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise ValueError("Git command failed: " + result.stderr[:1000])
    return result.stdout


def collect(inputs):
    repo = Path(inputs["repo_path"]).expanduser().resolve(strict=True)
    if not repo.is_dir():
        raise ValueError("repo_path must be a directory")
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    mode = inputs.get("scope", "staged")
    if mode not in ("staged", "working", "branch"):
        raise ValueError("scope must be staged, working or branch")
    head = git(repo, "rev-parse", "--verify", "HEAD").strip()
    args = ["--cached"] if mode == "staged" else []
    base = None
    if mode == "branch":
        ref = inputs.get("base_ref")
        if not isinstance(ref, str) or not ref or ref.startswith("-"):
            raise ValueError("branch scope requires a base_ref")
        base = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
        args = [base + "..." + head]
    diff = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--unified=3", *args, "--")
    if len(diff.encode("utf-8")) > 2_000_000:
        raise ValueError("Diff exceeds 2 MB; select a smaller change")
    names = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", *args, "--")
    files = [x for x in names.split("\0") if x]
    stat = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--stat", *args, "--")
    # Untracked files are reported, never silently included in a commit draft.
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    template = ""
    template_source = None
    for name in (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
                 ".gitcode/PULL_REQUEST_TEMPLATE.md", "PULL_REQUEST_TEMPLATE.md"):
        candidate = (repo / name).resolve()
        if candidate.is_relative_to(repo) and candidate.is_file():
            if candidate.stat().st_size > 64_000:
                raise ValueError("PR template exceeds 64 KB")
            template = candidate.read_text(encoding="utf-8")
            template_source = name
            break
    summary = inputs.get("summary", "")
    if not isinstance(summary, str) or "\n" in summary or len(summary) > 200:
        raise ValueError("summary must be a single line of at most 200 characters")
    kind = inputs.get("change_type", "chore")
    if kind not in ("feat", "fix", "docs", "refactor", "test", "chore", "perf", "build", "ci"):
        raise ValueError("Unsupported change_type")
    title = f"{kind}: {summary}" if summary else None
    return {"success": True, "scope": mode, "head_sha": head, "base_sha": base,
            "has_changes": bool(diff), "files": files, "diff": diff, "stat": stat,
            "untracked_excluded": [x for x in untracked if x],
            "template": template, "template_source": template_source,
            "commit_message": title, "pr_title": summary or None,
            "pr_body": template or "## 变更说明\n\n## 验证\n未提供执行证据。\n\n## 风险与限制\n",
            "draft": True, "needs_semantic_summary": not bool(summary),
            "note": "working compares tracked working-tree files to the index; untracked files are excluded. No tests were executed."}


class CommitDraftTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(id="commit_draft", name="commit_draft",
            description="读取 Git 差异和 PR 模板，为提交说明提供可核对证据；不提交、不推送。",
            input_params={"type": "object", "properties": {
                "repo_path": {"type": "string"},
                "scope": {"type": "string", "enum": ["staged", "working", "branch"]},
                "base_ref": {"type": "string"}, "summary": {"type": "string"},
                "change_type": {"type": "string"}}, "required": ["repo_path"]}))

    async def invoke(self, inputs, **kwargs):
        try:
            if not isinstance(inputs, dict):
                raise ValueError("Expected an object")
            return await asyncio.to_thread(collect, inputs)
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            return {"success": False, "error": str(exc)}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
